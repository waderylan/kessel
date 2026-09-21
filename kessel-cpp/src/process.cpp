#include "kessel/core.hpp"

#include <array>
#include <cstring>
#include <future>
#include <set>
#include <sstream>

#ifdef _WIN32
#include <windows.h>
#else
#include <csignal>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace kessel {

static const std::set<std::string> allowed_environment = {
    "APPDATA", "COMSPEC", "CODEX_HOME", "HOME", "HOMEDRIVE", "HOMEPATH",
    "LANG", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA", "LOGNAME", "PATH", "PATHEXT",
    "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USER",
    "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR", "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"};

std::map<std::string, std::string>
child_environment(const std::map<std::string, std::string>& overrides) {
  std::map<std::string, std::string> result;
#ifdef _WIN32
  LPWCH block = GetEnvironmentStringsW();
  if (block) {
    for (const wchar_t* entry = block; *entry; entry += std::wcslen(entry) + 1) {
      std::wstring item(entry); const auto equals = item.find(L'=');
      if (equals == std::wstring::npos || equals == 0) continue;
      std::string name(item.begin(), item.begin() + static_cast<std::ptrdiff_t>(equals));
      std::string upper = name; std::transform(upper.begin(), upper.end(), upper.begin(), ::toupper);
      if (allowed_environment.contains(upper) || upper.rfind("LC_", 0) == 0)
        result[name] = std::string(item.begin() + static_cast<std::ptrdiff_t>(equals + 1), item.end());
    }
    FreeEnvironmentStringsW(block);
  }
#else
  for (char** current = environ; current && *current; ++current) {
    std::string item(*current); const auto equals = item.find('='); if (equals == std::string::npos) continue;
    auto name = item.substr(0, equals); auto upper = name; std::transform(upper.begin(), upper.end(), upper.begin(), ::toupper);
    if (allowed_environment.contains(upper) || upper.rfind("LC_", 0) == 0) result[name] = item.substr(equals + 1);
  }
#endif
  result["NO_COLOR"] = "1";
  for (const auto& [name, value] : overrides) result[name] = value;
  return result;
}

static ProcessError classify_failure(std::string message, ProcessError::Kind fallback,
                                     std::string provider = {}) {
  const auto normalized = lower(message);
  if (!provider.empty() && (normalized.find("not logged in") != std::string::npos ||
      normalized.find("not authenticated") != std::string::npos ||
      normalized.find("authentication required") != std::string::npos ||
      normalized.find("please login") != std::string::npos ||
      normalized.find("please log in") != std::string::npos ||
      normalized.find("run /login") != std::string::npos)) {
    ProcessError error(message, ProcessError::Kind::authentication); error.provider = provider; return error;
  }
  if (normalized.find("rate limit") != std::string::npos ||
      normalized.find("rate_limit") != std::string::npos ||
      normalized.find("usage limit") != std::string::npos ||
      normalized.find("quota exhausted") != std::string::npos ||
      normalized.find("too many requests") != std::string::npos)
    return ProcessError(message, ProcessError::Kind::rate_limit);
  return ProcessError(message, fallback);
}

struct Capture {
  std::string retained;
  std::size_t total = 0;
  std::exception_ptr error;
};

static void consume_bytes(Capture& capture, const char* data, std::size_t size,
                          std::size_t retain, std::string& line_buffer,
                          const LineCallback& callback, std::atomic<bool>& abort) {
  capture.total += size;
  if (capture.retained.size() < retain) {
    const auto amount = std::min(size, retain - capture.retained.size());
    capture.retained.append(data, amount);
  }
  if (!callback) return;
  line_buffer.append(data, size);
  if (line_buffer.size() > 65'536 && line_buffer.find('\n') == std::string::npos)
    throw ProcessError("provider emitted an oversized output line", ProcessError::Kind::output_limit);
  std::size_t position = 0;
  while ((position = line_buffer.find('\n')) != std::string::npos) {
    auto line = line_buffer.substr(0, position);
    if (!line.empty() && line.back() == '\r') line.pop_back();
    line_buffer.erase(0, position + 1);
    if (!callback(line)) { abort = true; return; }
  }
}

#ifdef _WIN32

static std::wstring widen(std::string_view text) {
  if (text.empty()) return {};
  const int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
  std::wstring result(size, L'\0');
  MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size);
  return result;
}

static std::string narrow(std::wstring_view text) {
  if (text.empty()) return {};
  const int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
  std::string result(size, '\0');
  WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size, nullptr, nullptr);
  return result;
}

static std::wstring quote_windows(std::string_view argument) {
  auto value = widen(argument);
  if (value.find_first_of(L" \t\n\v\"") == std::wstring::npos) return value;
  std::wstring result = L"\""; std::size_t slashes = 0;
  for (wchar_t character : value) {
    if (character == L'\\') { ++slashes; continue; }
    if (character == L'\"') { result.append(slashes * 2 + 1, L'\\'); result += character; slashes = 0; continue; }
    result.append(slashes, L'\\'); slashes = 0; result += character;
  }
  result.append(slashes * 2, L'\\'); return result + L"\"";
}

static std::vector<wchar_t> environment_block(const std::map<std::string, std::string>& values) {
  std::vector<std::wstring> entries;
  for (const auto& [name, value] : values) entries.push_back(widen(name + "=" + value));
  std::sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) { return _wcsicmp(a.c_str(), b.c_str()) < 0; });
  std::vector<wchar_t> block;
  for (const auto& entry : entries) { block.insert(block.end(), entry.begin(), entry.end()); block.push_back(L'\0'); }
  block.push_back(L'\0'); return block;
}

ProcessResult run_process(const std::vector<std::string>& command,
                          std::string_view input, const fs::path& cwd,
                          int timeout_seconds, std::size_t max_output_bytes,
                          const std::map<std::string, std::string>& overrides,
                          const LineCallback& stdout_line) {
  if (command.empty()) throw ProcessError("empty command", ProcessError::Kind::not_found);
  const auto executable = find_executable(command[0]);
  if (!executable) throw ProcessError("command not found: " + command[0], ProcessError::Kind::not_found);
  SECURITY_ATTRIBUTES attributes{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
  HANDLE stdin_read = nullptr, stdin_write = nullptr, stdout_read = nullptr, stdout_write = nullptr,
         stderr_read = nullptr, stderr_write = nullptr;
  auto make_pipe = [&](HANDLE& read, HANDLE& write, bool parent_reads) {
    if (!CreatePipe(&read, &write, &attributes, 0)) throw ProcessError("CreatePipe failed");
    SetHandleInformation(parent_reads ? read : write, HANDLE_FLAG_INHERIT, 0);
  };
  make_pipe(stdin_read, stdin_write, false); make_pipe(stdout_read, stdout_write, true); make_pipe(stderr_read, stderr_write, true);
  STARTUPINFOW startup{}; startup.cb = sizeof(startup); startup.dwFlags = STARTF_USESTDHANDLES;
  startup.hStdInput = stdin_read; startup.hStdOutput = stdout_write; startup.hStdError = stderr_write;
  PROCESS_INFORMATION process{};
  std::wstring line;
  for (std::size_t index = 0; index < command.size(); ++index) { if (index) line += L' '; line += quote_windows(index == 0 ? executable->string() : command[index]); }
  auto env = environment_block(child_environment(overrides));
  auto working = cwd.wstring(); auto application = executable->wstring();
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (job) { JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{}; info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info, sizeof(info)); }
  const BOOL created = CreateProcessW(application.c_str(), line.data(), nullptr, nullptr, TRUE,
      CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT,
      env.data(), working.c_str(), &startup, &process);
  CloseHandle(stdin_read); CloseHandle(stdout_write); CloseHandle(stderr_write);
  if (!created) {
    CloseHandle(stdin_write); CloseHandle(stdout_read); CloseHandle(stderr_read); if (job) CloseHandle(job);
    throw ProcessError("could not start command: " + narrow(application), ProcessError::Kind::not_found);
  }
  if (job) AssignProcessToJobObject(job, process.hProcess);
  std::atomic<bool> abort = false;
  Capture out, err;
  auto reader = [&](HANDLE handle, Capture& capture, std::size_t retain, LineCallback callback) {
    try {
      std::array<char, 16'384> buffer{}; DWORD read = 0; std::string lines;
      while (ReadFile(handle, buffer.data(), buffer.size(), &read, nullptr) && read > 0) {
        consume_bytes(capture, buffer.data(), read, retain, lines, callback, abort);
        if (abort) break;
      }
      if (callback && !abort && !lines.empty()) callback(lines);
    } catch (...) { capture.error = std::current_exception(); abort = true; }
    CloseHandle(handle);
  };
  std::thread out_thread(reader, stdout_read, std::ref(out), max_output_bytes + 1, stdout_line);
  std::thread err_thread(reader, stderr_read, std::ref(err), static_cast<std::size_t>(65'536), LineCallback{});
  std::thread input_thread([&] { DWORD written = 0; std::size_t offset = 0;
    while (offset < input.size()) { const auto amount = static_cast<DWORD>(std::min<std::size_t>(input.size() - offset, 65'536)); if (!WriteFile(stdin_write, input.data() + offset, amount, &written, nullptr)) break; offset += written; }
    CloseHandle(stdin_write);
  });
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_seconds);
  bool timed_out = false;
  while (WaitForSingleObject(process.hProcess, 10) == WAIT_TIMEOUT) {
    if (abort) { if (job) TerminateJobObject(job, 1); else TerminateProcess(process.hProcess, 1); break; }
    if (std::chrono::steady_clock::now() >= deadline) { timed_out = true; if (job) TerminateJobObject(job, 1); else TerminateProcess(process.hProcess, 1); break; }
  }
  WaitForSingleObject(process.hProcess, INFINITE); DWORD exit_code = 1; GetExitCodeProcess(process.hProcess, &exit_code);
  input_thread.join(); out_thread.join(); err_thread.join();
  CloseHandle(process.hThread); CloseHandle(process.hProcess); if (job) CloseHandle(job);
  if (out.error) std::rethrow_exception(out.error);
  if (err.error) std::rethrow_exception(err.error);
  if (timed_out) throw ProcessError("provider exceeded " + std::to_string(timeout_seconds) + " second timeout", ProcessError::Kind::timeout);
  if (out.total + err.total > max_output_bytes) throw ProcessError("provider output exceeded " + std::to_string(max_output_bytes) + " bytes", ProcessError::Kind::output_limit);
  ProcessResult result{out.retained, err.retained, static_cast<int>(exit_code)};
  if (exit_code != 0 && !abort) { auto error = classify_failure(trim(result.err), ProcessError::Kind::exit); error.internal_detail = trim(result.err); throw error; }
  return result;
}

#else

ProcessResult run_process(const std::vector<std::string>& command,
                          std::string_view input, const fs::path& cwd,
                          int timeout_seconds, std::size_t max_output_bytes,
                          const std::map<std::string, std::string>& overrides,
                          const LineCallback& stdout_line) {
  if (command.empty()) throw ProcessError("empty command", ProcessError::Kind::not_found);
  const auto executable = find_executable(command[0]);
  if (!executable) throw ProcessError("command not found: " + command[0], ProcessError::Kind::not_found);
  std::vector<std::string> environment_storage;
  for (const auto& [name, value] : child_environment(overrides))
    environment_storage.push_back(name + "=" + value);
  std::vector<char*> arguments;
  for (const auto& item : command) arguments.push_back(const_cast<char*>(item.c_str()));
  arguments.push_back(nullptr);
  std::vector<char*> environment_values;
  for (auto& item : environment_storage) environment_values.push_back(item.data());
  environment_values.push_back(nullptr);
  int in_pipe[2], out_pipe[2], err_pipe[2];
  if (pipe(in_pipe)) throw ProcessError("pipe failed");
  if (pipe(out_pipe)) { close(in_pipe[0]); close(in_pipe[1]); throw ProcessError("pipe failed"); }
  if (pipe(err_pipe)) { close(in_pipe[0]); close(in_pipe[1]); close(out_pipe[0]); close(out_pipe[1]); throw ProcessError("pipe failed"); }
  static std::once_flag sigpipe_once;
  std::call_once(sigpipe_once, [] { std::signal(SIGPIPE, SIG_IGN); });
  const pid_t pid = fork();
  if (pid == 0) {
    setsid(); chdir(cwd.c_str()); dup2(in_pipe[0], STDIN_FILENO); dup2(out_pipe[1], STDOUT_FILENO); dup2(err_pipe[1], STDERR_FILENO);
    close(in_pipe[0]); close(in_pipe[1]); close(out_pipe[0]); close(out_pipe[1]);
    close(err_pipe[0]); close(err_pipe[1]);
    execve(executable->c_str(), arguments.data(), environment_values.data()); _exit(127);
  }
  if (pid < 0) {
    close(in_pipe[0]); close(in_pipe[1]); close(out_pipe[0]); close(out_pipe[1]);
    close(err_pipe[0]); close(err_pipe[1]); throw ProcessError("fork failed");
  }
  close(in_pipe[0]); close(out_pipe[1]); close(err_pipe[1]);
  std::atomic<bool> abort = false; Capture out, err;
  auto reader = [&](int descriptor, Capture& capture, std::size_t retain, LineCallback callback) { try { std::array<char, 16384> buffer{}; std::string lines; ssize_t count;
    while ((count = read(descriptor, buffer.data(), buffer.size())) > 0) { consume_bytes(capture, buffer.data(), count, retain, lines, callback, abort); if (abort) break; }
    if (callback && !abort && !lines.empty()) callback(lines); } catch (...) { capture.error = std::current_exception(); abort = true; } close(descriptor); };
  std::thread out_thread(reader, out_pipe[0], std::ref(out), max_output_bytes + 1, stdout_line);
  std::thread err_thread(reader, err_pipe[0], std::ref(err), static_cast<std::size_t>(65'536), LineCallback{});
  std::thread input_thread([&] { std::size_t offset = 0; while (offset < input.size()) { auto count = write(in_pipe[1], input.data() + offset, input.size() - offset); if (count <= 0) break; offset += count; } close(in_pipe[1]); });
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_seconds); int status = 0; bool timed_out = false;
  while (waitpid(pid, &status, WNOHANG) == 0) { if (abort || std::chrono::steady_clock::now() >= deadline) { timed_out = !abort; killpg(pid, SIGKILL); waitpid(pid, &status, 0); break; } std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
  input_thread.join(); out_thread.join(); err_thread.join();
  if (out.error) std::rethrow_exception(out.error);
  if (err.error) std::rethrow_exception(err.error);
  if (timed_out) throw ProcessError("provider exceeded " + std::to_string(timeout_seconds) + " second timeout", ProcessError::Kind::timeout);
  if (out.total + err.total > max_output_bytes) throw ProcessError("provider output exceeded " + std::to_string(max_output_bytes) + " bytes", ProcessError::Kind::output_limit);
  int code = WIFEXITED(status) ? WEXITSTATUS(status) : 1; ProcessResult result{out.retained, err.retained, code};
  if (code != 0 && !abort) { auto error = classify_failure(trim(result.err), ProcessError::Kind::exit); error.internal_detail = trim(result.err); throw error; } return result;
}

#endif

}  // namespace kessel
