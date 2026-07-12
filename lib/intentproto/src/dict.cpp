// intentproto dictionary builder: serialize the descriptor registry
// to the legacy identify JSON. This is a data->data transform — the
// input is the registered descriptors, never source code. Production
// firmware runs this in a host-side build tool and zlib-compresses
// the output into Config::identify_blob; it is also usable on-target
// where RAM allows.

#include "intentproto/proto.hpp"

namespace intentproto {

namespace {

struct Appender {
    char* out;
    size_t cap;
    size_t len;
    bool overflow;

    void raw(const char* s) {
        while (*s) {
            if (len >= cap) { overflow = true; return; }
            out[len++] = *s++;
        }
    }
    void ch(char c) {
        if (len >= cap) { overflow = true; return; }
        out[len++] = c;
    }
    void num(int32_t v) {
        char tmp[12];
        int i = 0;
        uint32_t u = (uint32_t)v;
        if (v < 0) { ch('-'); u = (uint32_t)(-(int64_t)v); }
        do { tmp[i++] = (char)('0' + u % 10); u /= 10; } while (u);
        while (i--) ch(tmp[i]);
    }
    // Config strings are not escaped: they must be JSON-safe.
    void quoted(const char* s) { ch('"'); raw(s); ch('"'); }
};

// "name param=%c param2=%u" — the dictionary key for a message.
void message_key(Appender& a, const char* name, const char* const* pnames,
                 const ParamType* ptypes, uint8_t n) {
    a.ch('"');
    a.raw(name);
    for (uint8_t i = 0; i < n; i++) {
        a.ch(' ');
        a.raw(pnames[i]);
        a.ch('=');
        a.raw(format_of(ptypes[i]));
    }
    a.ch('"');
}

} // namespace

size_t build_dictionary(char* out, size_t cap) {
    const Config& cfg = current_config();
    Appender a{out, cap, 0, false};

    a.raw("{\"build_version\":");
    a.quoted(cfg.build_version ? cfg.build_version : "");

    a.raw(",\"commands\":{");
    bool first = true;
    for (const Command* c = first_command(); c; c = c->next) {
        if (!first) a.ch(',');
        first = false;
        message_key(a, c->name, c->param_names, c->param_types,
                    c->num_params);
        a.ch(':');
        a.num(c->id);
    }
    a.ch('}');

    a.raw(",\"config\":{");
    first = true;
    for (const Constant* k = first_constant(); k; k = k->next) {
        if (!first) a.ch(',');
        first = false;
        a.quoted(k->name);
        a.ch(':');
        if (k->str_value)
            a.quoted(k->str_value);
        else
            a.num(k->int_value);
    }
    a.ch('}');

    // Consecutive records sharing an enum name form one object (the
    // declaration macro asks for values to be declared together).
    a.raw(",\"enumerations\":{");
    first = true;
    for (const Enumeration* e = first_enumeration(); e;) {
        if (!first) a.ch(',');
        first = false;
        const char* group = e->enum_name;
        a.quoted(group);
        a.raw(":{");
        bool first_value = true;
        for (; e && !strcmp(e->enum_name, group); e = e->next) {
            if (!first_value) a.ch(',');
            first_value = false;
            a.quoted(e->value_name);
            a.ch(':');
            a.num(e->value);
        }
        a.ch('}');
    }
    a.ch('}');

    a.raw(",\"responses\":{\"identify_response offset=%u data=%.*s\":0");
    for (const Response* r = first_response(); r; r = r->next) {
        a.ch(',');
        message_key(a, r->name, r->field_names, r->field_types,
                    r->num_fields);
        a.ch(':');
        a.num(r->id);
    }
    a.ch('}');

    a.raw(",\"version\":");
    a.quoted(cfg.version ? cfg.version : "");
    a.ch('}');

    if (a.overflow)
        return 0;
    if (a.len < cap)
        out[a.len] = '\0';
    return a.len;
}

} // namespace intentproto
