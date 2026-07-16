#ifndef __GENERIC_USB_CDC_EP_H
#define __GENERIC_USB_CDC_EP_H

// Default USB endpoint ids
enum {
    USB_CDC_EP_BULK_IN = 1,
    USB_CDC_EP_BULK_OUT = 2,
    USB_CDC_EP_ACM = 3,
    // Additional CDC-ACM data endpoints used by the Helix composite
    // gs_usb + control-console device.
    USB_CDC_LOCAL_EP_BULK_OUT = 4,
    USB_CDC_LOCAL_EP_BULK_IN = 5,
};

// Default endpoint sizes
enum {
    USB_CDC_EP0_SIZE = 16,
    USB_CDC_EP_ACM_SIZE = 8,
    USB_CDC_EP_BULK_OUT_SIZE = 64,
    USB_CDC_EP_BULK_IN_SIZE = 64,
};

#endif // usb_cdc_ep.h
