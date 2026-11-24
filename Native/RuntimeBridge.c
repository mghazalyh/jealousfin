#include "RuntimeBridge.h"
#include "quickjs.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/resource.h>
#include <mach/mach.h>

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

char *jf_runtime_eval_budget(const char *source, int32_t timeout_ms, int32_t memory_mib, int32_t *status) {
    if (!status) return NULL;
    *status = 1;
    if (!source || timeout_ms < 1 || timeout_ms > 10000 || memory_mib < 8 || memory_mib > 64 || strlen(source) > 4194304)
        return jf_message("Invalid interpreter input");
    JSRuntime *rt = JS_NewRuntime();
    if (!rt) return jf_message("Cannot allocate interpreter");
    JS_SetMemoryLimit(rt, (size_t)memory_mib * 1024 * 1024);
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
char *jf_runtime_eval(const char *source, int32_t timeout_ms, int32_t *status) {
    return jf_runtime_eval_budget(source, timeout_ms, 32, status);
}
int32_t jf_process_resources(uint64_t *footprint_bytes, uint64_t *cpu_microseconds) {
    if (!footprint_bytes || !cpu_microseconds) return 0;
    *footprint_bytes = 0; *cpu_microseconds = 0;
    int32_t flags = 0;
    task_vm_info_data_t vm;
    mach_msg_type_number_t count = TASK_VM_INFO_COUNT;
    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vm, &count) == KERN_SUCCESS) {
        *footprint_bytes = vm.phys_footprint; flags |= 1;
    }
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) == 0) {
        *cpu_microseconds = (uint64_t)usage.ru_utime.tv_sec * 1000000 + usage.ru_utime.tv_usec +
                            (uint64_t)usage.ru_stime.tv_sec * 1000000 + usage.ru_stime.tv_usec;
        flags |= 2;
    }
    return flags;
}
void jf_runtime_free(char *value) { free(value); }
