#ifndef JEALOUSFIN_RUNTIME_BRIDGE_H
#define JEALOUSFIN_RUNTIME_BRIDGE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// Returned UTF-8 string is owned by the caller. Free with jf_runtime_free.
char *jf_runtime_eval(const char *source, int32_t timeout_ms, int32_t *status);
char *jf_runtime_eval_budget(const char *source, int32_t timeout_ms, int32_t memory_mib, int32_t *status);
int32_t jf_process_resources(uint64_t *footprint_bytes, uint64_t *cpu_microseconds);
void jf_runtime_free(char *value);
#ifdef __cplusplus
}
#endif
#endif
