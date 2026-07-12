// Exercise the wide (>8-field) response maps and the zero-field
// KLIPPER_RESPONSE0 macro — the declaration-layer additions the
// OpenAMS port needed (oams_cmd_stats has 10 fields; the legacy
// "starting" ack is a zero-field response).

#include "intentproto/method.hpp"

#include <stdio.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

// 10-field response (exceeds the original 8-arg map ceiling).
KLIPPER_RESPONSE(wide_stats,
                 (uint32_t, a), (uint32_t, b), (uint32_t, c), (uint32_t, d),
                 (uint32_t, e), (uint32_t, f), (uint32_t, g), (uint32_t, h),
                 (uint32_t, i), (uint32_t, j));

// Zero-field response.
KLIPPER_RESPONSE0(starting);

static uint8_t g_tx[128];
static size_t g_tx_len;
static int test_write(const uint8_t* d, size_t n, void*) {
    if (g_tx_len + n <= sizeof(g_tx)) { memcpy(g_tx + g_tx_len, d, n); }
    g_tx_len += n;
    return (int)n;
}

static const intentproto::Response* res_by_name(const char* name) {
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next)
        if (!strcmp(r->name, name))
            return r;
    return nullptr;
}

int main() {
    intentproto::Config cfg;
    cfg.write = test_write;
    intentproto::init(cfg);

    const intentproto::Response* w = res_by_name("wide_stats");
    CHECK(w != nullptr);
    CHECK(w->num_fields == 10);
    CHECK(w->field_types[9] == intentproto::ParamType::U32);
    CHECK(!strcmp(w->field_names[9], "j"));

    const intentproto::Response* s = res_by_name("starting");
    CHECK(s != nullptr);
    CHECK(s->num_fields == 0);

    // Both must actually pack/send without overrun.
    g_tx_len = 0;
    intentproto::reply(wide_stats{1, 2, 3, 4, 5, 6, 7, 8, 9, 10});
    CHECK(g_tx_len >= 10);        // 10 vlq fields + framing
    g_tx_len = 0;
    intentproto::reply(starting{});
    CHECK(g_tx_len == intentproto::MESSAGE_MIN + 1);  // msgid only

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
