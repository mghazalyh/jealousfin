#include "RuntimeBridge.h"
#include "quickjs.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct { double deadline; int timed_out; } JFDeadline;
static double jf_now(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}
static int jf_interrupt(JSRuntime *rt, void *opaque) {
    (void)rt;
    JFDeadline *d = opaque;
    if (jf_now() >= d->deadline) {
        d->timed_out = 1;
        return 1;
    }
    return 0;
}
static char *jf_copy(const char *value, size_t len) {
    if (!value || len > 1048576) return NULL;
    char *result = malloc(len + 1);
    if (!result) return NULL;
    memcpy(result, value, len);
    result[len] = '\0';
    return result;
}
static char *jf_message(const char *value) { return jf_copy(value, strlen(value)); }

char *jf_runtime_eval(const char *source, int32_t timeout_ms, int32_t *status) {
    if (!status) return NULL;
    *status = 1;
    if (!source || timeout_ms < 1 || timeout_ms > 10000 || strlen(source) > 4194304)
        return jf_message("Invalid interpreter input");
    JSRuntime *rt = JS_NewRuntime();
    if (!rt) return jf_message("Cannot allocate interpreter");
    JS_SetMemoryLimit(rt, 32 * 1024 * 1024);
    JS_SetMaxStackSize(rt, 128 * 1024);
    JFDeadline deadline = { jf_now() + (double)timeout_ms / 1000.0, 0 };
    JS_SetInterruptHandler(rt, jf_interrupt, &deadline);
    JSContext *ctx = JS_NewContext(rt);
    if (!ctx) {
        JS_FreeRuntime(rt);
        return jf_message("Cannot allocate interpreter context");
    }
    JSValue value = JS_Eval(ctx, source, strlen(source), "jealousfin-module.js", JS_EVAL_TYPE_GLOBAL);
    char *result = NULL;
    if (deadline.timed_out) {
        *status = 2;
        result = jf_message("JavaScript deadline exceeded");
    } else if (JS_IsException(value)) {
        JSValue exc = JS_GetException(ctx);
        size_t len = 0;
        const char *message = JS_ToCStringLen(ctx, &len, exc);
        result = jf_copy(message, len > 4096 ? 4096 : len);
        if (message) JS_FreeCString(ctx, message);
        JS_FreeValue(ctx, exc);
        if (deadline.timed_out) {
            free(result);
            result = jf_message("JavaScript deadline exceeded");
            *status = 2;
        }
        if (!result) result = jf_message("JavaScript exception");
    } else {
        size_t len = 0;
        const char *message = JS_ToCStringLen(ctx, &len, value);
        result = jf_copy(message, len);
        if (message) JS_FreeCString(ctx, message);
        if (deadline.timed_out) {
            free(result);
            result = jf_message("JavaScript deadline exceeded");
            *status = 2;
        } else if (result) {
            *status = 0;
        } else {
            result = jf_message("Interpreter result exceeds limit or cannot be encoded");
        }
    }
    JS_FreeValue(ctx, value);
    JS_FreeContext(ctx);
    JS_FreeRuntime(rt);
    return result;
}
void jf_runtime_free(char *value) { free(value); }
