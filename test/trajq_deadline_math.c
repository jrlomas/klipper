// Test adapter for the exact constant-divide helpers used by the quintic
// pulse-crossing evaluator.  Unused command/queue sections are discarded by
// the shared-library test build.
#include <stdint.h>

#include "../src/trajq.c"

int64_t
helix_test_sdiv64_120(int64_t value)
{
    return sdiv64_120(value);
}

int32_t
helix_test_sdiv64_24_to_s32(int64_t value)
{
    return sdiv64_24_to_s32(value);
}

int64_t
helix_test_smul_shr_deadline(int64_t value, uint32_t ticks, uint32_t shift)
{
    return smul_shr_deadline(value, ticks, shift);
}

int64_t
helix_test_scale_i32_deadline(int32_t value, uint32_t factor)
{
    return scale_i32_deadline(value, factor);
}
