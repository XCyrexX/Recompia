// SPDX-License-Identifier: GPL-3.0-or-later
// The runtime's own files on the host: the folder the game's executable is in, and a file a release ships next to it
// (src/host_files.cpp).
#pragma once

#include <filesystem>

namespace nxrt {

// The folder of the running executable (pkp.exe, or the native pkp). On POSIX through the compat layer's GetModuleFileNameA
// (/proc/self/exe on Linux, _NSGetExecutablePath on macOS). Empty if the host does not say.
std::filesystem::path host_exe_dir();

// A file a release ships for the runtime, `<folder>/<name>`: the first that exists of <exe folder>/<folder>/<name> (a package and
// what Set up places next to the game), <working directory>/assets/<folder>/<name> (a run from a checkout) and <exe folder>/<name>.
// Empty if none does.
std::filesystem::path host_asset(const char* folder, const char* name);

}  // namespace nxrt
