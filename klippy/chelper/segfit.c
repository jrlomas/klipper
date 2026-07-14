// Greedy quadratic segment fitter for trajectory intentions.
//
// Samples a joint's position trajectory q(t) through the existing
// kinematics callback chain (kinematics, input shaper, and pressure
// advance included for free) and fits chained quadratic segments
// within a configured deviation tolerance. Coefficient quantization
// is part of the fit: the *quantized* polynomial is evaluated
// against the sampled trajectory, so wire rounding can never push
// the executed path outside tolerance (FD-0001 docs 02/05).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <math.h> // round
#include <stddef.h> // offsetof
#include <stdint.h> // int64_t
#include <stdlib.h> // malloc
#include <string.h> // memset
#include "compiler.h" // __visible
#include "itersolve.h" // struct stepper_kinematics
#include "list.h" // list_node
#include "trapq.h" // struct move

#define SEGFIT_MAX_DURATION ((double)(1 << 26))
#define SEGFIT_MAX_SAMPLES 4096
#define SEGFIT_MAX_SEGS 256

struct segfit_seg {
    uint32_t duration;
    int32_t velocity;
    int32_t accel;
    uint8_t flags;
};

struct segfit {
    struct stepper_kinematics *sk;
    double mcu_freq;          // ticks per second
    double su_per_mm;         // sub-units per mm (65536 / microstep dist)
    double position_offset_su;// commanded joint space -> physical MCU space
    double tolerance;         // max deviation, in sub-units
    uint32_t sample_ticks;    // sampling quantum
    uint8_t cruise_fastpath;  // motion may spend tolerance to reach accel=0
    // Chained anchor: exact Q32.32 sub-unit position and the print
    // time / tick count it corresponds to.
    double anchor_print_time;
    int64_t acc;             // modulo-2^64 Q32.32 wire phase
    double anchor_su;        // unwrapped physical position in sub-units
    uint64_t gen_ticks;       // ticks fitted since the anchor
    // Sample buffer for the segment currently being grown
    double *tau, *y;          // ticks since segment start, su offset
    int num_samples;
    // Incremental least-squares sums (fit constrained through 0,0)
    double s2, s3, s4, sy1, sy2;
    // Output
    struct segfit_seg segs[SEGFIT_MAX_SEGS];
    int num_segs;
};

struct segfit * __visible
segfit_alloc(void)
{
    struct segfit *sf = malloc(sizeof(*sf));
    memset(sf, 0, sizeof(*sf));
    sf->tau = malloc(sizeof(double) * SEGFIT_MAX_SAMPLES);
    sf->y = malloc(sizeof(double) * SEGFIT_MAX_SAMPLES);
    return sf;
}

void __visible
segfit_free(struct segfit *sf)
{
    if (!sf)
        return;
    free(sf->tau);
    free(sf->y);
    free(sf);
}

void __visible
segfit_setup(struct segfit *sf, struct stepper_kinematics *sk
             , double mcu_freq, double su_per_mm, double tolerance_su
             , double sample_time)
{
    sf->sk = sk;
    sf->mcu_freq = mcu_freq;
    sf->su_per_mm = su_per_mm;
    sf->tolerance = tolerance_su;
    uint32_t st = (uint32_t)round(sample_time * mcu_freq);
    sf->sample_ticks = st ? st : 1;
}

void __visible
segfit_set_anchor(struct segfit *sf, double print_time, int64_t acc)
{
    sf->anchor_print_time = print_time;
    sf->acc = acc;
    sf->anchor_su = acc / 4294967296.;
    sf->gen_ticks = 0;
    sf->num_samples = 0;
    sf->num_segs = 0;
    sf->s2 = sf->s3 = sf->s4 = sf->sy1 = sf->sy2 = 0.;
}

void __visible
segfit_set_anchor_position(struct segfit *sf, double position_su)
{
    sf->anchor_su = position_su;
}

void __visible
segfit_set_position_offset(struct segfit *sf, double offset_su)
{
    sf->position_offset_su = offset_su;
}

void __visible
segfit_set_cruise_fastpath(struct segfit *sf, uint8_t enable)
{
    sf->cruise_fastpath = !!enable;
}

int64_t __visible
segfit_get_anchor(struct segfit *sf)
{
    return sf->acc;
}

double __visible
segfit_get_gen_time(struct segfit *sf)
{
    return sf->anchor_print_time + sf->gen_ticks / sf->mcu_freq;
}

// ---- exact integer chaining (identical convention to src/trajq.c) ----

static int64_t
mul64x32_half(int64_t a, uint32_t b)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * b;
    uint64_t hi = (ua >> 32) * b;
    hi += lo >> 32;
    lo &= 0xffffffff;
    uint64_t r = (hi << 31) | (lo >> 1);
    return neg ? -(int64_t)r : (int64_t)r;
}

static int64_t
traj_end_delta(uint32_t duration, int32_t velocity, int32_t accel)
{
    int64_t delta = (int64_t)(
        (uint64_t)((int64_t)velocity * duration) << 16);
    if (accel)
        delta += mul64x32_half((int64_t)accel * duration, duration);
    return delta;
}

// ---- higher-order (cubic/quintic) exact chaining ----
// These MUST stay bit-for-bit identical to src/trajq.c (smul_shr,
// poly_term, trajq_end_delta_seg). See the range-analysis comment block
// there and FD-0001 doc 02 for the fixed-point scaling. jerk is stored
// * 2^48, snap * 2^64, crackle * 2^80.

static int64_t
smul_shr(int64_t a, uint32_t t, unsigned sh)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * t;
    uint64_t hi = (ua >> 32) * t;
    hi += lo >> 32;
    lo &= 0xffffffff;
    // host mirror: physical coefficients never trip the MCU overflow
    // guard, so no shutdown() here - the arithmetic below is identical.
    uint64_t r = sh ? ((hi << (32 - sh)) | (lo >> sh)) : ((hi << 32) | lo);
    return neg ? -(int64_t)r : (int64_t)r;
}

static int64_t
poly_term(int64_t coeff, uint32_t t, int nmul, int nsh, uint32_t fact)
{
    int64_t p = coeff;
    int i;
    for (i = 0; i < nmul; i++)
        p = smul_shr(p, t, i < nsh ? 16 : 0);
    if (fact > 1) {
        int neg = p < 0;
        uint64_t up = neg ? -(uint64_t)p : (uint64_t)p;
        up /= fact;
        p = neg ? -(int64_t)up : (int64_t)up;
    }
    return p;
}

// Exact Q32.32 end-of-segment delta for a cubic (snap=crackle=0) or
// quintic segment. Unused coefficients pass as zero.
int64_t __visible
segfit_end_delta_ho(uint32_t duration, int32_t velocity, int32_t accel
                    , int32_t jerk, int32_t snap, int32_t crackle)
{
    int64_t d = traj_end_delta(duration, velocity, accel);
    d += poly_term(jerk, duration, 3, 1, 6);
    d += poly_term(snap, duration, 4, 2, 24);
    d += poly_term(crackle, duration, 5, 3, 120);
    return d;
}

// ---- trajectory sampling ----

// Position (in mm) of the joint at an absolute print time, walking
// the trapezoid queue from a caller-held cursor.
static double
sample_position(struct segfit *sf, struct move **pm, double print_time)
{
    struct stepper_kinematics *sk = sf->sk;
    struct trapq *tq = sk->tq;
    struct move *m = *pm;
    if (!m) {
        if (list_empty(&tq->moves))
            return sk->commanded_pos;
        m = list_first_entry(&tq->moves, struct move, node);
    }
    // Advance to the move covering print_time
    // Keep an exact end timestamp on the completed move.  The following
    // sentinel may carry coordinate zero, and treating that sentinel as the
    // endpoint would manufacture a position discontinuity.  Contiguous real
    // moves have the same boundary position, so either side is equivalent
    // there; the completed side is the safe choice for a terminal flush.
    while (print_time > m->print_time + m->move_t) {
        if (list_is_last(&m->node, &tq->moves))
            break;
        struct move *next = list_next_entry(m, node);
        if (next->print_time > print_time) {
            // Gap between moves: hold at the end of m
            break;
        }
        m = next;
    }
    *pm = m;
    double move_time = print_time - m->print_time;
    if (move_time < 0.)
        move_time = 0.;
    if (move_time > m->move_t)
        move_time = m->move_t;
    return sk->calc_position_cb(sk, m, move_time);
}

// Return the queued joint position at an absolute print time.  A trajectory
// stream must anchor to the path it is about to fit, not to sk->commanded_pos:
// that legacy field is normally advanced by itersolve_generate_steps(), which
// trajectory steppers deliberately bypass.  The distinction is observable
// after a homing/probing halt or SET_KINEMATIC_POSITION, where a new trapq may
// begin at a nonzero position while the legacy solver still holds an older
// value.
double __visible
segfit_get_position(struct segfit *sf, double print_time)
{
    struct move *cursor = NULL;
    return sample_position(sf, &cursor, print_time);
}

// ---- fitting ----

// Solve the constrained least squares fit q(tau) = v*tau + beta*tau^2
static void
fit_coeffs(struct segfit *sf, double *v, double *beta)
{
    if (sf->num_samples == 1) {
        *v = sf->y[0] / sf->tau[0];
        *beta = 0.;
        return;
    }
    double det = sf->s2 * sf->s4 - sf->s3 * sf->s3;
    if (fabs(det) < 1e-30) {
        *v = sf->sy1 / sf->s2;
        *beta = 0.;
        return;
    }
    *v = (sf->sy1 * sf->s4 - sf->sy2 * sf->s3) / det;
    *beta = (sf->sy2 * sf->s2 - sf->sy1 * sf->s3) / det;
}

// Evaluate the QUANTIZED candidate against every sample in the span.
// Returns 1 if all deviations are inside tolerance.
static int
check_fit(struct segfit *sf, int n, int32_t vw, int32_t aw)
{
    double vq = vw / 65536., aq = aw / 4294967296.;
    int i;
    for (i = 0; i < n; i++) {
        double t = sf->tau[i];
        double q = vq * t + .5 * aq * t * t;
        double err = q - sf->y[i];
        if (err > sf->tolerance || err < -sf->tolerance)
            return 0;
    }
    return 1;
}

static void
quantize(double v, double beta, int32_t *vw, int32_t *aw)
{
    double vd = round(v * 65536.);
    double ad = round(2. * beta * 4294967296.);
    if (vd > 2147483647.) vd = 2147483647.;
    if (vd < -2147483648.) vd = -2147483648.;
    if (ad > 2147483647.) ad = 2147483647.;
    if (ad < -2147483648.) ad = -2147483648.;
    *vw = (int32_t)vd;
    *aw = (int32_t)ad;
}

// Prefer the cheaper pure-velocity realization whenever it satisfies the
// same quantized error budget.  This is exact for cruise spans and avoids
// manufacturing tiny corrective accelerations solely from chained fixed-
// point rounding.  Trajectory stepper MCUs can realize this common case with
// one closed-form crossing solve instead of iterative quadratic roots.
static void
prefer_pure_velocity(struct segfit *sf, int n, double s2, double sy1,
                     int32_t *vw, int32_t *aw)
{
    if (!*aw || s2 == 0.)
        return;
    int32_t pure_v, pure_a;
    quantize(sy1 / s2, 0., &pure_v, &pure_a);
    double endpoint = pure_v / 65536. * sf->tau[n - 1];
    // Motion fitting may spend its configured path-error budget to eliminate
    // tiny acceleration coefficients after a ramp.  Value/PWM fitting keeps
    // a tighter endpoint condition so independently acceptable chunks do not
    // accumulate visible final-value drift.
    if ((sf->cruise_fastpath
         || fabs(endpoint - sf->y[n - 1]) <= 32.)
        && check_fit(sf, n, pure_v, 0)) {
        *vw = pure_v;
        *aw = 0;
    }
}

// The velocity sign may not change inside a segment (protocol
// invariant). Returns 1 if (vw, aw, T) complies.
static int
check_dir_invariant(int32_t vw, int32_t aw, uint32_t T)
{
    int64_t vend = vw + (((int64_t)aw * T) >> 16);
    if ((vw > 0 && vend < 0) || (vw < 0 && vend > 0))
        return 0;
    return 1;
}

// Emit the first n samples of the current span as one segment;
// shift the remainder down to start a new span.
static int
emit_segment(struct segfit *sf, int n)
{
    if (sf->num_segs >= SEGFIT_MAX_SEGS)
        return -1;
    uint32_t T = (uint32_t)sf->tau[n - 1];
    double v, beta;
    int32_t vw, aw;
    // Refit over just the emitted span
    double s2 = 0., s3 = 0., s4 = 0., sy1 = 0., sy2 = 0.;
    int i;
    for (i = 0; i < n; i++) {
        double t = sf->tau[i], q = sf->y[i];
        double t2 = t * t;
        s2 += t2; s3 += t2 * t; s4 += t2 * t2;
        sy1 += t * q; sy2 += t2 * q;
    }
    double det = s2 * s4 - s3 * s3;
    if (n == 1 || fabs(det) < 1e-30) {
        v = n ? sy1 / s2 : 0.;
        beta = 0.;
    } else {
        v = (sy1 * s4 - sy2 * s3) / det;
        beta = (sy2 * s2 - sy1 * s3) / det;
    }
    quantize(v, beta, &vw, &aw);
    prefer_pure_velocity(sf, n, s2, sy1, &vw, &aw);
    while (!check_dir_invariant(vw, aw, T) && n > 1) {
        // Trim back to before the extremum and retry
        n--;
        T = (uint32_t)sf->tau[n - 1];
        s2 = s3 = s4 = sy1 = sy2 = 0.;
        for (i = 0; i < n; i++) {
            double t = sf->tau[i], q = sf->y[i];
            double t2 = t * t;
            s2 += t2; s3 += t2 * t; s4 += t2 * t2;
            sy1 += t * q; sy2 += t2 * q;
        }
        det = s2 * s4 - s3 * s3;
        if (n == 1 || fabs(det) < 1e-30) {
            v = sy1 / s2;
            beta = 0.;
        } else {
            v = (sy1 * s4 - sy2 * s3) / det;
            beta = (sy2 * s2 - sy1 * s3) / det;
        }
        quantize(v, beta, &vw, &aw);
        prefer_pure_velocity(sf, n, s2, sy1, &vw, &aw);
    }
    if (!check_dir_invariant(vw, aw, T))
        // Single-sample segment still reversing: force pure velocity
        aw = 0;
    // Quantization clamps coefficients to the signed wire range.  A target
    // outside that range must fail closed: emitting the clamped candidate
    // would turn a position discontinuity into a maximum-rate pulse burst.
    if ((vw == INT32_MIN || vw == INT32_MAX
         || aw == INT32_MIN || aw == INT32_MAX)
        && !check_fit(sf, n, vw, aw))
        return -1;

    struct segfit_seg *seg = &sf->segs[sf->num_segs++];
    seg->duration = T;
    seg->velocity = vw;
    seg->accel = aw;
    seg->flags = 0;

    // Advance the exact chained anchor with the integer convention
    int64_t delta = traj_end_delta(T, vw, aw);
    sf->acc = (int64_t)((uint64_t)sf->acc + (uint64_t)delta);
    sf->anchor_su += delta / 4294967296.;
    sf->gen_ticks += T;

    // Re-express any remaining samples relative to the new anchor
    double dsu = delta / 4294967296.;
    int rem = sf->num_samples - n;
    sf->s2 = sf->s3 = sf->s4 = sf->sy1 = sf->sy2 = 0.;
    for (i = 0; i < rem; i++) {
        double t = sf->tau[n + i] - T;
        double q = sf->y[n + i] - dsu;
        sf->tau[i] = t;
        sf->y[i] = q;
        double t2 = t * t;
        sf->s2 += t2; sf->s3 += t2 * t; sf->s4 += t2 * t2;
        sf->sy1 += t * q; sf->sy2 += t2 * q;
    }
    sf->num_samples = rem;
    return 0;
}

// Fit the trajectory forward to flush_time. Returns the number of
// segments produced (drain with segfit_get_segs), or -1 on overflow.
int __visible
segfit_generate(struct segfit *sf, double flush_time)
{
    sf->num_segs = 0;
    double anchor_su = sf->anchor_su;
    uint64_t window_end =
        (uint64_t)((flush_time - sf->anchor_print_time) * sf->mcu_freq);
    struct move *cursor = NULL;
    for (;;) {
        uint64_t last_sample_tick = sf->gen_ticks;
        if (sf->num_samples)
            last_sample_tick += (uint64_t)sf->tau[sf->num_samples - 1];
        uint64_t next_tick = last_sample_tick + sf->sample_ticks;
        int final_partial = 0;
        if (next_tick > window_end) {
            if (last_sample_tick >= window_end)
                break;
            // A flush horizon need not land on the sampling grid.  Include
            // that exact endpoint so finalize() cannot leave up to one
            // sample interval of motion unrepresented.
            next_tick = window_end;
            final_partial = 1;
        }
        double t_print = sf->anchor_print_time + next_tick / sf->mcu_freq;
        double q_mm = sample_position(sf, &cursor, t_print);
        double q_su = (q_mm * sf->su_per_mm + sf->position_offset_su
                       - anchor_su);
        // Sample relative to the current segment start
        double tau = (double)(next_tick - sf->gen_ticks);
        double q = q_su;
        int n = sf->num_samples;
        sf->tau[n] = tau;
        sf->y[n] = q;
        sf->num_samples = n + 1;
        double t2 = tau * tau;
        sf->s2 += t2; sf->s3 += t2 * tau; sf->s4 += t2 * t2;
        sf->sy1 += tau * q; sf->sy2 += t2 * q;

        // Check the grown span still fits within tolerance
        double v, beta;
        int32_t vw, aw;
        fit_coeffs(sf, &v, &beta);
        quantize(v, beta, &vw, &aw);
        prefer_pure_velocity(sf, sf->num_samples, sf->s2, sf->sy1,
                             &vw, &aw);
        int ok = check_fit(sf, sf->num_samples, vw, aw);
        if ((!ok && sf->num_samples > 1)
            || tau >= SEGFIT_MAX_DURATION
            || sf->num_samples >= SEGFIT_MAX_SAMPLES) {
            int emit_n = ok ? sf->num_samples : sf->num_samples - 1;
            if (emit_segment(sf, emit_n))
                return -1;
            anchor_su = sf->anchor_su;
            if (sf->num_segs >= SEGFIT_MAX_SEGS)
                break;
        }
        if (final_partial)
            break;
    }
    return sf->num_segs;
}

// Flush any partial span as a final segment (used before an
// expected idle period so the joint lands exactly on its target).
int __visible
segfit_finalize(struct segfit *sf)
{
    sf->num_segs = 0;
    if (sf->num_samples)
        if (emit_segment(sf, sf->num_samples))
            return -1;
    return sf->num_segs;
}

struct segfit_seg * __visible
segfit_get_segs(struct segfit *sf)
{
    return sf->segs;
}
