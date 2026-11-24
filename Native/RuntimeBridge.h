#ifndef JEALOUSFIN_RUNTIME_BRIDGE_H
#define JEALOUSFIN_RUNTIME_BRIDGE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// Returned UTF-8 string is owned by the caller. Free with jf_runtime_free.
char *jf_runtime_eval(const char *source, int32_t timeout_ms, int32_t *status);
void jf_runtime_free(char *value);
#ifdef __cplusplus
}
#endif
#endif
