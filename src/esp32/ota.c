// ESP32 in-band update over ESP-IDF OTA (FD-0001 doc 11).
//
// The protocol commands are identical to every other target's —
// enter_bootloader / flash_begin / flash_data / flash_verify /
// flash_boot, ordinary Class-1 traffic — but underneath there is no
// separate bootloader image: the running application streams the new
// image into the inactive OTA partition and asks the IDF second-stage
// bootloader to boot it. The A/B partitions ARE the unbrickable
// story here (esp_ota_end validates the image; a bad set_boot rolls
// back), so this maps the same commands onto esp_ota_begin/write/
// end/set_boot_partition instead of bootcore's raw FlashOps.
//
// Compiled under idf.py against the real IDF OTA headers (the esp32
// port has no host build). Gated by CONFIG_WANT_BOOTLOADER.

#include "autoconf.h" // CONFIG_WANT_BOOTLOADER

#if CONFIG_WANT_BOOTLOADER

#include <string.h> // memset
#include "esp_ota_ops.h" // esp_ota_begin
#include "esp_partition.h" // esp_partition_t
#include "esp_system.h" // esp_restart
#include "command.h" // DECL_COMMAND

// flash_result op codes (shared vocabulary with boot_main.cpp).
enum { OP_BEGIN = 0, OP_DATA, OP_VERIFY, OP_BOOT, OP_ENTER };
// result codes: 0 == OK; non-zero mirrors bootcore's error taxonomy.
enum {
    R_OK = 0, R_STATE = 1, R_RANGE = 2, R_ORDER = 3, R_FLASH = 4,
    R_CRC = 5,
};

static struct {
    esp_ota_handle_t handle;
    const esp_partition_t *part;
    uint32_t size;        // announced image size
    uint32_t received;    // contiguous high-water mark
    uint32_t crc_want;    // whole-image CRC32 the host announced
    uint32_t crc_acc;     // running (pre-final) CRC32 register
    uint8_t active;       // a transfer is in progress
} ota;

// Streaming CRC-32 (IEEE 802.3, reflected) — the same polynomial as
// intentproto::crc32, kept as a running register so the whole-image
// value can be checked without buffering the image.
static void
crc_begin(void)
{
    ota.crc_acc = 0xffffffff;
}
static void
crc_update(const uint8_t *data, uint32_t len)
{
    uint32_t crc = ota.crc_acc;
    while (len--) {
        crc ^= *data++;
        for (int i = 0; i < 8; i++)
            crc = (crc >> 1) ^ (0xedb88320 & (0 - (crc & 1)));
    }
    ota.crc_acc = crc;
}
static uint32_t
crc_final(void)
{
    return ~ota.crc_acc;
}

static void
ota_reset(void)
{
    memset(&ota, 0, sizeof(ota));
}

// flash_begin size=%u crc32=%u
void
command_flash_begin(uint32_t *args)
{
    uint32_t size = args[0], crc = args[1];
    // A host may reconnect after timing out while the partition erase was in
    // progress.  Explicitly abort that abandoned handle before starting a new
    // transfer; merely clearing our bookkeeping would leak the OTA operation
    // and make the retry fail.
    if (ota.active)
        esp_ota_abort(ota.handle);
    ota_reset();
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) {
        sendf("flash_result op=%c code=%c arg=%u", OP_BEGIN, R_FLASH, 0);
        return;
    }
    if (!size || size > part->size) {
        sendf("flash_result op=%c code=%c arg=%u", OP_BEGIN, R_RANGE,
              part->size);
        return;
    }
    esp_err_t err = esp_ota_begin(part, size, &ota.handle);
    if (err != ESP_OK) {
        sendf("flash_result op=%c code=%c arg=%u", OP_BEGIN, R_FLASH,
              (uint32_t)err);
        return;
    }
    ota.part = part;
    ota.size = size;
    ota.crc_want = crc;
    ota.received = 0;
    ota.active = 1;
    crc_begin();
    sendf("flash_result op=%c code=%c arg=%u", OP_BEGIN, R_OK, size);
}
DECL_COMMAND(command_flash_begin, "flash_begin size=%u crc32=%u");

// flash_data offset=%u data=%*s — contiguous, ack-windowed.
void
command_flash_data(uint32_t *args)
{
    uint32_t offset = args[0];
    uint8_t len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);
    if (!ota.active) {
        sendf("flash_result op=%c code=%c arg=%u", OP_DATA, R_STATE, 0);
        return;
    }
    if (offset != ota.received) {
        sendf("flash_result op=%c code=%c arg=%u", OP_DATA, R_ORDER,
              ota.received);
        return;
    }
    if (offset + len > ota.size) {
        sendf("flash_result op=%c code=%c arg=%u", OP_DATA, R_RANGE, 0);
        return;
    }
    esp_err_t err = esp_ota_write(ota.handle, data, len);
    if (err != ESP_OK) {
        ota.active = 0;
        sendf("flash_result op=%c code=%c arg=%u", OP_DATA, R_FLASH,
              (uint32_t)err);
        return;
    }
    crc_update(data, len);
    ota.received = offset + len;
    sendf("flash_result op=%c code=%c arg=%u", OP_DATA, R_OK, ota.received);
}
DECL_COMMAND(command_flash_data, "flash_data offset=%u data=%*s");

// flash_verify — finish the OTA (IDF validates the image) and check
// the whole-image CRC the host announced.
void
command_flash_verify(uint32_t *args)
{
    (void)args;
    if (!ota.active || ota.received != ota.size) {
        sendf("flash_result op=%c code=%c arg=%u", OP_VERIFY, R_STATE,
              ota.received);
        return;
    }
    if (crc_final() != ota.crc_want) {
        esp_ota_abort(ota.handle);
        ota.active = 0;
        sendf("flash_result op=%c code=%c arg=%u", OP_VERIFY, R_CRC, 0);
        return;
    }
    esp_err_t err = esp_ota_end(ota.handle);
    if (err != ESP_OK) {
        ota.active = 0;
        sendf("flash_result op=%c code=%c arg=%u", OP_VERIFY, R_FLASH,
              (uint32_t)err);
        return;
    }
    sendf("flash_result op=%c code=%c arg=%u", OP_VERIFY, R_OK,
          ota.crc_want);
}
DECL_COMMAND(command_flash_verify, "flash_verify");

// flash_boot — select the new partition and restart into it. The IDF
// bootloader boots it; a first boot that never calls
// esp_ota_mark_app_valid_cancel_rollback() rolls back (if rollback is
// enabled in sdkconfig) — the A/B unbrickable path.
void
command_flash_boot(uint32_t *args)
{
    (void)args;
    if (!ota.part) {
        sendf("flash_result op=%c code=%c arg=%u", OP_BOOT, R_STATE, 0);
        return;
    }
    esp_err_t err = esp_ota_set_boot_partition(ota.part);
    if (err != ESP_OK) {
        sendf("flash_result op=%c code=%c arg=%u", OP_BOOT, R_FLASH,
              (uint32_t)err);
        return;
    }
    sendf("flash_result op=%c code=%c arg=%u", OP_BOOT, R_OK, 0);
    esp_restart();
}
DECL_COMMAND(command_flash_boot, "flash_boot");

// enter_bootloader force=%c — on ESP32 the running app already accepts
// OTA, so there is no separate bootloader to reset into; acknowledge
// that we are ready to take flash_begin.
void
command_enter_bootloader(uint32_t *args)
{
    (void)args;
    sendf("flash_result op=%c code=%c arg=%u", OP_ENTER, R_OK, 0);
}
DECL_COMMAND_FLAGS(command_enter_bootloader, HF_IN_SHUTDOWN,
                   "enter_bootloader force=%c");

#endif // CONFIG_WANT_BOOTLOADER
