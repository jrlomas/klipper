#ifndef INTENTPROTO_METHOD_HPP
#define INTENTPROTO_METHOD_HPP
// intentproto declaration layer — annotation-style static registration.
//
// Usage (at namespace scope of one .cpp file):
//
//     KLIPPER_RESPONSE(oams_action_status,
//                      (uint8_t, action), (uint8_t, code), (uint32_t, value));
//
//     KLIPPER_METHOD(oams_cmd_load_spool, (uint8_t, spool)) {
//         if (busy) {
//             intentproto::reply(oams_action_status{ACTION_LOAD, ERR_BUSY, 0});
//             return;
//         }
//         start_load(spool);
//     }
//
//     KLIPPER_METHOD0(oams_cmd_unload_spool) {
//         start_unload();
//     }
//
//     KLIPPER_CONSTANT(CLOCK_FREQ, 48000000);
//     KLIPPER_CONSTANT_STR(MCU, "oams-stm32f072rbt6");
//
// That is the complete surface: no table, no registration call, no
// build step. Each macro defines the user's function/struct exactly
// as written and drops a static descriptor next to it; the
// descriptor's constructor links itself into the library's registry
// before main(). Parameter TYPES are deduced from the function
// signature by the Thunk template (so they can never drift from the
// code); parameter NAMES appear once in the macro because C++ has no
// reflection over parameter names.
//
// Everything here is compile-time or static-storage machinery: no
// heap, no exceptions, no RTTI, no virtual dispatch.
//
// Caveat: declare each method/response in exactly one translation
// unit (they are internal-linkage definitions; a header would
// register duplicates).

#include "proto.hpp"

#include <type_traits>
#include <utility>

namespace intentproto {

// Map a C++ parameter type to its wire type.
template <typename T>
constexpr ParamType wire_type() {
    using U = typename std::remove_cv<T>::type;
    static_assert(std::is_same<U, uint8_t>::value ||
                  std::is_same<U, int8_t>::value ||
                  std::is_same<U, uint16_t>::value ||
                  std::is_same<U, int16_t>::value ||
                  std::is_same<U, uint32_t>::value ||
                  std::is_same<U, int32_t>::value ||
                  std::is_same<U, bool>::value ||
                  std::is_same<U, buf>::value,
                  "unsupported wire parameter type");
    return std::is_same<U, buf>::value      ? ParamType::Buf
         : std::is_same<U, bool>::value     ? ParamType::Bool
         : std::is_same<U, uint8_t>::value  ? ParamType::U8
         : std::is_same<U, int8_t>::value   ? ParamType::I8
         : std::is_same<U, uint16_t>::value ? ParamType::U16
         : std::is_same<U, int16_t>::value  ? ParamType::I16
         : std::is_same<U, int32_t>::value  ? ParamType::I32
                                            : ParamType::U32;
}

namespace detail {

// ArgWords a parameter occupies: integers one, buf two (length then
// pointer — the ArgWord convention documented in proto.hpp).
template <typename T>
constexpr size_t arg_words() {
    return std::is_same<typename std::remove_cv<T>::type, buf>::value
        ? 2 : 1;
}

// Args arrive sign-extended in a 32-bit value; narrow per the
// handler's real parameter type. Takes a pointer because a buf
// parameter spans two words.
template <typename T>
inline T decode_arg(const ArgWord* w) {
    if (std::is_same<typename std::remove_cv<T>::type, bool>::value)
        return (T)(*w != 0);
    return (T)(int32_t)(uint32_t)*w;
}

template <>
inline buf decode_arg<buf>(const ArgWord* w) {
    return buf{(const uint8_t*)(uintptr_t)w[1], (uint32_t)w[0]};
}

// Thunk<&fn>: deduces the handler's parameter types from its
// signature and provides the descriptor pieces — the wire-type array
// and a uniform invoke(args[]) trampoline that unpacks and calls it.
template <auto F, typename Sig>
struct ThunkImpl;

template <auto F, typename... A>
struct ThunkImpl<F, void (*)(A...)> {
    static constexpr uint8_t count = (uint8_t)sizeof...(A);
    // One extra slot so the zero-parameter case stays a valid array.
    static constexpr ParamType types[sizeof...(A) + 1] = { wire_type<A>()... };
    static void invoke(const ArgWord* args) {
        call(args, std::index_sequence_for<A...>{});
    }
private:
    // ArgWord offset of parameter idx (buf parameters occupy two).
    static constexpr size_t offset(size_t idx) {
        constexpr size_t w[sizeof...(A) + 1] = { arg_words<A>()..., 0 };
        size_t o = 0;
        for (size_t j = 0; j < idx; j++)
            o += w[j];
        return o;
    }
    template <size_t... I>
    static void call(const ArgWord* args, std::index_sequence<I...>) {
        (void)args;
        F(decode_arg<A>(args + offset(I))...);
    }
};

} // namespace detail

template <auto F>
using Thunk = detail::ThunkImpl<F, decltype(F)>;

} // namespace intentproto

// ---------- preprocessor plumbing (pairs and small maps) ----------

#define IP_CAT_(a, b) a##b
#define IP_CAT(a, b) IP_CAT_(a, b)
#define IP_STR_(x) #x
#define IP_STR(x) IP_STR_(x)

// A parameter is written as a (type, name) pair; these split it.
#define IP_PAIR_TYPE_(t, n) t
#define IP_PAIR_NAME_(t, n) n
#define IP_PAIR_TYPE(p) IP_PAIR_TYPE_ p
#define IP_PAIR_NAME(p) IP_PAIR_NAME_ p
#define IP_PAIR_NAMESTR(p) IP_STR(IP_PAIR_NAME(p))
#define IP_PAIR_DECL(p) IP_PAIR_TYPE(p) IP_PAIR_NAME(p)
#define IP_PAIR_WTYPE(p) ::intentproto::wire_type<IP_PAIR_TYPE(p)>()
#define IP_PACK_FIELD(p) w.put(r.IP_PAIR_NAME(p))

#define IP_NARG(...) IP_NARG_(__VA_ARGS__, 8, 7, 6, 5, 4, 3, 2, 1, 0)
#define IP_NARG_(_1, _2, _3, _4, _5, _6, _7, _8, N, ...) N

// Comma-separated map over up to 8 pairs.
#define IP_M1(M, a) M(a)
#define IP_M2(M, a, ...) M(a), IP_M1(M, __VA_ARGS__)
#define IP_M3(M, a, ...) M(a), IP_M2(M, __VA_ARGS__)
#define IP_M4(M, a, ...) M(a), IP_M3(M, __VA_ARGS__)
#define IP_M5(M, a, ...) M(a), IP_M4(M, __VA_ARGS__)
#define IP_M6(M, a, ...) M(a), IP_M5(M, __VA_ARGS__)
#define IP_M7(M, a, ...) M(a), IP_M6(M, __VA_ARGS__)
#define IP_M8(M, a, ...) M(a), IP_M7(M, __VA_ARGS__)
#define IP_MAP(M, ...) IP_CAT(IP_M, IP_NARG(__VA_ARGS__))(M, __VA_ARGS__)

// Semicolon-terminated map (struct fields, statements).
#define IP_S1(M, a) M(a);
#define IP_S2(M, a, ...) M(a); IP_S1(M, __VA_ARGS__)
#define IP_S3(M, a, ...) M(a); IP_S2(M, __VA_ARGS__)
#define IP_S4(M, a, ...) M(a); IP_S3(M, __VA_ARGS__)
#define IP_S5(M, a, ...) M(a); IP_S4(M, __VA_ARGS__)
#define IP_S6(M, a, ...) M(a); IP_S5(M, __VA_ARGS__)
#define IP_S7(M, a, ...) M(a); IP_S6(M, __VA_ARGS__)
#define IP_S8(M, a, ...) M(a); IP_S7(M, __VA_ARGS__)
#define IP_MAPS(M, ...) IP_CAT(IP_S, IP_NARG(__VA_ARGS__))(M, __VA_ARGS__)

// ---------- the annotations ----------

// Command with parameters: KLIPPER_METHOD(name, (type, name), ...) { body }
#define KLIPPER_METHOD(fname, ...)                                          \
    static void fname(IP_MAP(IP_PAIR_DECL, __VA_ARGS__));                   \
    namespace {                                                             \
    const char* const IP_CAT(_ip_pn_, fname)[] = {                          \
        IP_MAP(IP_PAIR_NAMESTR, __VA_ARGS__)                                \
    };                                                                      \
    ::intentproto::Command IP_CAT(_ip_cmd_, fname){                         \
        #fname, IP_CAT(_ip_pn_, fname),                                     \
        ::intentproto::Thunk<&fname>::types,                                \
        ::intentproto::Thunk<&fname>::count,                                \
        &::intentproto::Thunk<&fname>::invoke};                             \
    }                                                                       \
    static void fname(IP_MAP(IP_PAIR_DECL, __VA_ARGS__))

// Command without parameters: KLIPPER_METHOD0(name) { body }
#define KLIPPER_METHOD0(fname)                                              \
    static void fname();                                                    \
    namespace {                                                             \
    ::intentproto::Command IP_CAT(_ip_cmd_, fname){                         \
        #fname, nullptr, nullptr, 0,                                        \
        &::intentproto::Thunk<&fname>::invoke};                             \
    }                                                                       \
    static void fname()

// Response struct: KLIPPER_RESPONSE(name, (type, field), ...);
// Defines `struct name {...}` plus its descriptor and pack function;
// send one with intentproto::reply(name{...}).
#define KLIPPER_RESPONSE(rname, ...)                                        \
    struct rname { IP_MAPS(IP_PAIR_DECL, __VA_ARGS__) };                    \
    namespace {                                                             \
    const char* const IP_CAT(_ip_rn_, rname)[] = {                          \
        IP_MAP(IP_PAIR_NAMESTR, __VA_ARGS__)                                \
    };                                                                      \
    constexpr ::intentproto::ParamType IP_CAT(_ip_rt_, rname)[] = {         \
        IP_MAP(IP_PAIR_WTYPE, __VA_ARGS__)                                  \
    };                                                                      \
    void IP_CAT(_ip_pack_, rname)(::intentproto::Writer& w,                 \
                                  const void* pv) {                         \
        const rname& r = *static_cast<const rname*>(pv);                    \
        IP_MAPS(IP_PACK_FIELD, __VA_ARGS__)                                 \
    }                                                                       \
    ::intentproto::Response IP_CAT(_ip_res_, rname){                        \
        #rname, IP_CAT(_ip_rn_, rname), IP_CAT(_ip_rt_, rname),             \
        (uint8_t)(sizeof(IP_CAT(_ip_rn_, rname)) /                          \
                  sizeof(IP_CAT(_ip_rn_, rname)[0])),                       \
        &IP_CAT(_ip_pack_, rname)};                                         \
    }                                                                       \
    static inline ::intentproto::Response& _ip_desc_of(const rname*) {      \
        return IP_CAT(_ip_res_, rname);                                     \
    }                                                                       \
    static_assert(true, "")  /* swallow the trailing semicolon */

// Dictionary constants: KLIPPER_CONSTANT(CLOCK_FREQ, 48000000);
#define KLIPPER_CONSTANT(cname, value)                                      \
    namespace {                                                             \
    ::intentproto::Constant IP_CAT(_ip_const_, cname){#cname,               \
                                                      (int32_t)(value)};    \
    }                                                                       \
    static_assert(true, "")

#define KLIPPER_CONSTANT_STR(cname, value)                                  \
    namespace {                                                             \
    ::intentproto::Constant IP_CAT(_ip_const_, cname){#cname, value};       \
    }                                                                       \
    static_assert(true, "")

// Enumeration values: KLIPPER_ENUMERATION(spi_bus, spi0, 0);
// Declare all values of one enumeration consecutively — the
// dictionary builder groups consecutive records sharing the
// enumeration name into a single "enumerations" object.
#define KLIPPER_ENUMERATION(ename, vname, value)                            \
    namespace {                                                             \
    ::intentproto::Enumeration IP_CAT(IP_CAT(_ip_enum_, ename),             \
                                      IP_CAT(_, vname)){                    \
        #ename, #vname, (int32_t)(value)};                                  \
    }                                                                       \
    static_assert(true, "")

#endif // INTENTPROTO_METHOD_HPP
