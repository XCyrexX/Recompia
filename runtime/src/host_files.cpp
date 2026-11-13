// SPDX-License-Identifier: GPL-3.0-or-later
// The runtime's own files on the host (include/nxrt/host_files.h).
#include "nxrt/host_files.h"

#include <windows.h>   // compat/windows.h on POSIX: MAX_PATH, DWORD and the GetModuleFileNameA shim

#include <system_error>

namespace nxrt {

std::filesystem::path host_exe_dir() {
    char buf[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameA(nullptr, buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return {};
    return std::filesystem::path(buf).parent_path();
}

std::filesystem::path host_asset(const char* folder, const char* name) {
    std::error_code ec;
    const std::filesystem::path ex = host_exe_dir();
    std::filesystem::path cands[3];
    size_t n = 0;
    if (!ex.empty()) cands[n++] = ex / folder / name;
    cands[n++] = std::filesystem::current_path(ec) / "assets" / folder / name;
    if (!ex.empty()) cands[n++] = ex / name;
    for (size_t i = 0; i < n; ++i)
        if (std::filesystem::is_regular_file(cands[i], ec)) return cands[i];
    return {};
}

}  // namespace nxrt
