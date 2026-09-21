#include "kessel/core.hpp"

#include <algorithm>
#include <deque>
#include <fstream>
#include <iostream>
#include <set>

#ifdef _WIN32
#include <windows.h>
#else
#include <cerrno>
#include <csignal>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace kessel {

static const std::vector<std::string> disabled_features = {
    "apps", "browser_use", "computer_use", "goals", "hooks", "image_generation",
    "multi_agent", "plugins", "remote_plugin", "shell_tool",
    "skill_mcp_dependency_install", "skill_search", "sleep_tool", "tool_suggest",
    "unified_exec", "view_image"};

static bool default_model(std::string_view provider, std::string_view model) {
  return lower(std::string(model)) == "default" || lower(std::string(model)) == provider;
}

static Usage codex_usage(const json& raw) {
  return {raw.value("input_tokens", 0), raw.value("output_tokens", 0),
          raw.value("cached_input_tokens", 0)};
}

static Usage claude_usage(const json& raw) {
  const int cached = raw.value("cache_read_input_tokens", 0);
  return {raw.value("input_tokens", 0) + cached, raw.value("output_tokens", 0), cached};
}

static fs::path temporary_directory(std::string_view prefix) {
  for (int attempt = 0; attempt < 20; ++attempt) {
    auto path = fs::temp_directory_path() / (std::string(prefix) + random_hex(8));
    std::error_code error; if (fs::create_directory(path, error)) return path;
  }
  throw ProcessError("could not create provider temporary directory");
}

class DirectoryGuard {
 public:
  explicit DirectoryGuard(fs::path path) : path_(std::move(path)) {}
  ~DirectoryGuard() { std::error_code error; fs::remove_all(path_, error); }
  const fs::path& path() const { return path_; }
 private: fs::path path_;
};

class CodexProvider final : public Provider {
 public:
  explicit CodexProvider(const Settings& settings)
      : command_(settings.codex_command), timeout_(settings.request_timeout_seconds),
        max_output_(settings.max_output_bytes) {}
  std::string name() const override { return "codex"; }
  std::string command() const override { return command_; }

  ProviderResult complete(const ChatRequest& request, const LineCallback& delta) override {
    if (request.backend == "warm") return warm_complete(request, delta);
    DirectoryGuard directory(temporary_directory("kessel-codex-"));
    auto command = build_command(request, directory.path());
    std::string full_text; std::optional<Usage> usage; bool completed = false;
    bool consumer_stopped = false;
    auto consume = [&](std::string_view line) {
      if (trim(std::string(line)).empty()) return true;
      json event; try { event = json::parse(line); } catch (...) { throw ProcessError("Codex returned invalid JSONL output"); }
      const auto type = event.value("type", "");
      if (type == "item.updated" || type == "item.completed") {
        const auto item = event.value("item", json::object());
        if (item.value("type", "") == "agent_message") {
          const auto text = item.value("text", ""); std::string piece;
          if (text.rfind(full_text, 0) == 0) piece = text.substr(full_text.size()); else piece = text;
          if (text.rfind(full_text, 0) == 0) full_text = text; else full_text += text;
          if (!piece.empty() && delta && !delta(piece)) { consumer_stopped = true; return false; }
        }
      } else if (type == "turn.completed") { usage = codex_usage(event.value("usage", json::object())); completed = true; }
      else if (type == "turn.failed" || type == "error") {
        auto message = event.value("message", ""); if (message.empty() && event.contains("error")) message = event["error"].value("message", "Codex reported an error");
        auto normalized = lower(message); auto kind = (normalized.find("rate limit") != std::string::npos || normalized.find("usage limit") != std::string::npos) ? ProcessError::Kind::rate_limit : ProcessError::Kind::generic;
        throw ProcessError(message.empty() ? "Codex reported an error" : message, kind);
      }
      return true;
    };
    auto process = run_process(command, build_prompt(request), directory.path(), timeout_, max_output_, {}, consume);
    if ((!completed && !consumer_stopped) || full_text.empty()) throw ProcessError("Codex completed without an assistant message");
    ProviderResult result; result.text = full_text; result.model = request.model;
    result.usage = usage;
    result = parse_structured_result(request, std::move(result)); observe_model(result.model); return result;
  }

  std::vector<std::string> models() override;
  bool accepts_model(const std::string& model) override {
    if (Provider::accepts_model(model)) return true;
    const auto available = models(); return std::find(available.begin(), available.end(), model) != available.end();
  }
  void close() override;

 private:
  std::vector<std::string> build_command(const ChatRequest& request, const fs::path& cwd) const {
    const auto instruction_path = asset_root() / "resources" / "codex_instructions.txt";
    std::vector<std::string> command = {command_, "exec", "--json", "--ephemeral",
      "--ignore-user-config", "--ignore-rules", "--config",
      "model_instructions_file=" + json(instruction_path.generic_string()).dump(),
      "--config", "skills.max_context_tokens=1", "--config", "agents.enabled=false",
      "--config", "model_reasoning_effort=\"" + request.reasoning_effort + "\"",
      "--config", "model_reasoning_summary=\"none\"", "--config", "model_verbosity=\"low\"",
      "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never"};
    for (const auto& feature : disabled_features) { command.push_back("--disable"); command.push_back(feature); }
    if (request.service_tier == "fast") command.insert(command.end(), {"--enable", "fast_mode", "--config", "service_tier=\"fast\""});
    if (const auto schema = output_schema(request)) { const auto path = cwd / "output-schema.json"; std::ofstream(path) << schema->dump(); command.insert(command.end(), {"--output-schema", path.string()}); }
    if (!default_model("codex", request.model)) command.insert(command.end(), {"--model", request.model});
    command.push_back("-"); return command;
  }

  ProviderResult warm_complete(const ChatRequest& request, const LineCallback& delta);
  struct WarmServer;
  std::unique_ptr<WarmServer> warm_;
  std::mutex warm_mutex_;
  std::string command_; int timeout_; std::size_t max_output_;
};

class ClaudeProvider final : public Provider {
 public:
  explicit ClaudeProvider(const Settings& settings)
      : command_(settings.claude_command), timeout_(settings.request_timeout_seconds),
        max_output_(settings.max_output_bytes) {}
  std::string name() const override { return "claude"; }
  std::string command() const override { return command_; }
  bool accepts_model(const std::string& model) override {
    static const std::set<std::string> aliases = {"sonnet", "opus", "haiku", "fable", "mythos"};
    return Provider::accepts_model(model) || aliases.contains(model);
  }
  ProviderResult complete(const ChatRequest& request, const LineCallback& delta) override {
    if (request.backend == "warm") throw Error("Warm mode is only available for Codex. Claude stream-json keeps conversation state and cannot provide stateless requests.", 400, "invalid_request");
    DirectoryGuard directory(temporary_directory("kessel-claude-"));
    auto command = build_command(output_schema(request).has_value());
    std::map<std::string, std::string> env{{"CLAUDE_CODE_EFFORT_LEVEL", request.reasoning_effort}};
    if (!default_model("claude", request.model)) env["ANTHROPIC_MODEL"] = request.model;
    ProviderResult result;
    if (output_schema(request)) {
      const auto process = run_process(command, build_prompt(request), directory.path(), timeout_, max_output_, env);
      json payload; try { payload = json::parse(process.out); } catch (...) { throw ProcessError("Claude returned invalid JSON output"); }
      if (payload.value("is_error", false)) throw ProcessError(payload.value("result", "Claude reported an error"));
      if (payload.contains("structured_output") && !payload["structured_output"].is_null()) result.text = json_compact(payload["structured_output"]);
      else if (payload.contains("result") && payload["result"].is_string()) result.text = payload["result"].get<std::string>();
      result.model = request.model; if (payload.contains("usage") && payload["usage"].is_object()) result.usage = claude_usage(payload["usage"]);
    } else {
      std::string full_text; std::optional<ProviderResult> final; bool consumer_stopped = false;
      auto consume = [&](std::string_view line) {
        if (trim(std::string(line)).empty()) return true;
        json event;
        try { event = json::parse(line); } catch (...) { throw ProcessError("Claude returned invalid JSONL output"); }
        if (event.value("type", "") == "stream_event") {
          auto inner = event.value("event", json::object()); auto change = inner.value("delta", json::object());
          if (inner.value("type", "") == "content_block_delta" && change.value("type", "") == "text_delta") {
            auto text = change.value("text", ""); full_text += text; if (!text.empty() && delta && !delta(text)) { consumer_stopped = true; return false; }
          }
        } else if (event.value("type", "") == "system" && event.value("subtype", "") == "api_retry" && event.value("error", "") == "rate_limit") {
          ProcessError error("Claude rate limit reached", ProcessError::Kind::rate_limit); if (event.contains("retry_delay_ms") && event["retry_delay_ms"].is_number()) error.retry_after = std::max(1, event["retry_delay_ms"].get<int>() / 1000); throw error;
        } else if (event.value("type", "") == "result") {
          if (event.value("is_error", false)) throw ProcessError(event.value("result", "Claude reported an error"));
          ProviderResult parsed; parsed.text = event.value("result", full_text); parsed.model = request.model;
          if (event.contains("modelUsage") && event["modelUsage"].is_object() && !event["modelUsage"].empty()) parsed.model = event["modelUsage"].begin().key();
          if (event.contains("usage") && event["usage"].is_object())
            parsed.usage = claude_usage(event["usage"]);
          final = parsed;
        }
        return true;
      };
      run_process(command, build_prompt(request), directory.path(), timeout_, max_output_, env, consume);
      if (consumer_stopped && !full_text.empty()) {
        result.text = full_text; result.model = request.model;
      }
      else { if (!final || !final->text || final->text->empty()) throw ProcessError("Claude completed without an assistant message"); result = std::move(*final); }
    }
    result = parse_structured_result(request, std::move(result)); observe_model(result.model); return result;
  }
  std::vector<std::string> models() override { std::lock_guard lock(observed_mutex_); auto result = observed_models_; std::sort(result.begin(), result.end()); return result; }
 private:
  std::vector<std::string> build_command(bool structured) const {
    std::vector<std::string> command = {command_, "--print", "--output-format", structured ? "json" : "stream-json"};
    if (!structured) command.insert(command.end(), {"--verbose", "--include-partial-messages"});
    command.insert(command.end(), {"--no-session-persistence", "--permission-prompts", "none", "--safe-mode", "--restricted", "--tools", "", "--system-prompt", "You are a stateless text assistant. Answer the supplied conversation directly and concisely. Do not use tools.", "-"});
    return command;
  }
  std::string command_; int timeout_; std::size_t max_output_;
};

// Warm Codex uses one persistent app-server. Requests remain stateless because
// every call creates a new ephemeral thread. Notifications are routed by
// thread id, allowing independent turns to share the process concurrently.
struct CodexProvider::WarmServer {
  explicit WarmServer(std::string command, int timeout, std::size_t max_output)
      : command(std::move(command)), timeout(timeout), max_output(max_output) {}
  ~WarmServer() { stop(); }
  ProviderResult complete(const ChatRequest& request, const LineCallback& delta);
  std::vector<std::string> models();
  void stop();
 private:
  json rpc(const std::string& method, const json& params);
  void start();
  void write(const json& payload);
  std::optional<json> next_notification(const std::string& thread_id,
                                        std::chrono::steady_clock::time_point deadline);
  void reader_loop();
  std::string command; int timeout; std::size_t max_output;
  fs::path runtime;
  std::mutex start_mutex, models_mutex, write_mutex, state_mutex;
  std::condition_variable state_changed;
  int next_id = 1; bool failed = false; std::atomic<bool> stopping = false;
  std::string failure;
  std::unordered_map<int, json> responses;
  std::deque<json> notifications;
#ifdef _WIN32
  void* process_handle = nullptr; void* job_handle = nullptr; void* stdin_write = nullptr; void* stdout_read = nullptr; void* stderr_read = nullptr;
#else
  int pid = -1, stdin_fd = -1, stdout_fd = -1, stderr_fd = -1;
#endif
  std::thread reader, stderr_reader;
};

#ifdef _WIN32
static std::wstring warm_widen(std::string_view text) {
  int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0); std::wstring result(size, L'\0'); MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size); return result;
}
static std::wstring warm_quote(std::string_view value) {
  auto input = warm_widen(value); if (input.find_first_of(L" \t\"") == std::wstring::npos) return input;
  std::wstring result = L"\""; std::size_t slashes = 0; for (auto c : input) { if (c == L'\\') { ++slashes; continue; } if (c == L'\"') { result.append(slashes * 2 + 1, L'\\'); result += c; slashes = 0; } else { result.append(slashes, L'\\'); slashes = 0; result += c; } } result.append(slashes * 2, L'\\'); return result + L"\"";
}
static std::vector<wchar_t> warm_environment_block(
    const std::map<std::string, std::string>& values) {
  std::vector<std::wstring> entries;
  for (const auto& [name, value] : values)
    entries.push_back(warm_widen(name + "=" + value));
  std::sort(entries.begin(), entries.end(), [](const auto& left, const auto& right) {
    return _wcsicmp(left.c_str(), right.c_str()) < 0;
  });
  std::vector<wchar_t> block;
  for (const auto& entry : entries) {
    block.insert(block.end(), entry.begin(), entry.end());
    block.push_back(L'\0');
  }
  block.push_back(L'\0');
  return block;
}

void CodexProvider::WarmServer::start() {
  std::lock_guard startup(start_mutex);
  if (process_handle) return;
  auto executable = find_executable(command); if (!executable) throw ProcessError("command not found: " + command, ProcessError::Kind::not_found);
  runtime = temporary_directory("kessel-codex-server-"); fs::create_directory(runtime / "home");
  auto source_home = fs::path(environment("CODEX_HOME", environment("USERPROFILE") + "\\.codex"));
  std::error_code copy_error; if (fs::exists(source_home / "auth.json")) fs::copy_file(source_home / "auth.json", runtime / "home" / "auth.json", fs::copy_options::overwrite_existing, copy_error);
  std::vector<std::string> args = {executable->string(), "app-server", "--stdio", "--config", "skills.max_context_tokens=1", "--config", "agents.enabled=false", "--config", "mcp_servers={}", "--config", "model_reasoning_summary=\"none\"", "--config", "model_verbosity=\"low\""};
  for (const auto& feature : disabled_features) args.insert(args.end(), {"--disable", feature});
  SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE}; HANDLE in_read, in_write, out_read, out_write, err_read, err_write; CreatePipe(&in_read, &in_write, &sa, 0); CreatePipe(&out_read, &out_write, &sa, 0); CreatePipe(&err_read, &err_write, &sa, 0); SetHandleInformation(in_write, HANDLE_FLAG_INHERIT, 0); SetHandleInformation(out_read, HANDLE_FLAG_INHERIT, 0); SetHandleInformation(err_read, HANDLE_FLAG_INHERIT, 0);
  STARTUPINFOW si{}; si.cb = sizeof(si); si.dwFlags = STARTF_USESTDHANDLES; si.hStdInput = in_read; si.hStdOutput = out_write; si.hStdError = err_write; PROCESS_INFORMATION pi{};
  std::wstring line; for (std::size_t i = 0; i < args.size(); ++i) { if (i) line += L' '; line += warm_quote(args[i]); }
  auto environment = warm_environment_block(
      child_environment({{"CODEX_HOME", (runtime / "home").string()}}));
  BOOL ok = CreateProcessW(executable->wstring().c_str(), line.data(), nullptr, nullptr,
                           TRUE, CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP |
                                     CREATE_UNICODE_ENVIRONMENT,
                           environment.data(), runtime.wstring().c_str(), &si, &pi);
  CloseHandle(in_read); CloseHandle(out_write); CloseHandle(err_write);
  if (!ok) { CloseHandle(in_write); CloseHandle(out_read); CloseHandle(err_read); fs::remove_all(runtime); throw ProcessError("could not start Codex App Server"); }
  HANDLE job = CreateJobObjectW(nullptr, nullptr); if (job) { JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{}; info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info, sizeof(info)); AssignProcessToJobObject(job, pi.hProcess); }
  CloseHandle(pi.hThread); process_handle = pi.hProcess; job_handle = job; stdin_write = in_write; stdout_read = out_read; stderr_read = err_read; failed = false; stopping = false;
  reader = std::thread([this] { reader_loop(); });
  stderr_reader = std::thread([this] { std::array<char, 16384> buffer{}; DWORD count; std::size_t total = 0; while (ReadFile(static_cast<HANDLE>(stderr_read), buffer.data(), buffer.size(), &count, nullptr) && count) { total += count; if (total > max_output) { std::lock_guard lock(state_mutex); failed = true; failure = "provider output exceeded configured limit"; state_changed.notify_all(); if (job_handle) TerminateJobObject(static_cast<HANDLE>(job_handle), 1); break; } } });
  try {
    rpc("initialize", {{"clientInfo", {{"name", "kessel_cpp"}, {"title", "Kessel C++"}, {"version", KESSEL_CPP_VERSION}}}});
    write({{"method", "initialized"}, {"params", json::object()}});
  } catch (...) { stop(); throw; }
}

void CodexProvider::WarmServer::write(const json& payload) {
  auto text = json_compact(payload) + "\n"; std::lock_guard lock(write_mutex); DWORD written = 0;
  if (!WriteFile(static_cast<HANDLE>(stdin_write), text.data(), static_cast<DWORD>(text.size()), &written, nullptr) || written != text.size()) throw ProcessError("Codex App Server closed stdin");
}

void CodexProvider::WarmServer::reader_loop() {
  std::array<char, 16384> buffer{}; DWORD count; std::string pending;
  while (ReadFile(static_cast<HANDLE>(stdout_read), buffer.data(), buffer.size(), &count, nullptr) && count) {
    pending.append(buffer.data(), count); std::size_t newline;
    while ((newline = pending.find('\n')) != std::string::npos) { auto line = pending.substr(0, newline); pending.erase(0, newline + 1); try { auto message = json::parse(line); std::lock_guard lock(state_mutex); if (message.contains("id") && message["id"].is_number_integer()) { const int id = message["id"].get<int>(); responses[id] = std::move(message); } else notifications.push_back(std::move(message)); state_changed.notify_all(); } catch (const std::exception& error) { std::cerr << "Codex App Server emitted invalid JSON: " << error.what() << '\n'; } }
  }
  if (!stopping) { std::lock_guard lock(state_mutex); failed = true; failure = "Codex App Server exited unexpectedly"; state_changed.notify_all(); }
}

void CodexProvider::WarmServer::stop() {
  stopping = true; if (stdin_write) { CloseHandle(static_cast<HANDLE>(stdin_write)); stdin_write = nullptr; }
  if (process_handle) { if (WaitForSingleObject(static_cast<HANDLE>(process_handle), 1000) == WAIT_TIMEOUT) { if (job_handle) TerminateJobObject(static_cast<HANDLE>(job_handle), 1); else TerminateProcess(static_cast<HANDLE>(process_handle), 1); } WaitForSingleObject(static_cast<HANDLE>(process_handle), INFINITE); }
  if (reader.joinable()) reader.join();
  if (stderr_reader.joinable()) stderr_reader.join();
  if (stdout_read) CloseHandle(static_cast<HANDLE>(stdout_read));
  if (stderr_read) CloseHandle(static_cast<HANDLE>(stderr_read));
  if (process_handle) CloseHandle(static_cast<HANDLE>(process_handle));
  if (job_handle) CloseHandle(static_cast<HANDLE>(job_handle));
  stdout_read = stderr_read = process_handle = job_handle = nullptr; std::error_code error; if (!runtime.empty()) fs::remove_all(runtime, error); runtime.clear();
}
#else
void CodexProvider::WarmServer::start() {
  std::lock_guard startup(start_mutex);
  if (pid > 0) return;
  auto executable = find_executable(command);
  if (!executable) throw ProcessError("command not found: " + command,
                                      ProcessError::Kind::not_found);
  runtime = temporary_directory("kessel-codex-server-");
  fs::create_directory(runtime / "home");
  auto source_home = fs::path(environment("CODEX_HOME",
                                          environment("HOME") + "/.codex"));
  std::error_code copy_error;
  if (fs::exists(source_home / "auth.json"))
    fs::copy_file(source_home / "auth.json", runtime / "home" / "auth.json",
                  fs::copy_options::overwrite_existing, copy_error);
  std::vector<std::string> args = {
      executable->string(), "app-server", "--stdio", "--config",
      "skills.max_context_tokens=1", "--config", "agents.enabled=false",
      "--config", "mcp_servers={}", "--config",
      "model_reasoning_summary=\"none\"", "--config", "model_verbosity=\"low\""};
  for (const auto& feature : disabled_features)
    args.insert(args.end(), {"--disable", feature});
  auto environment_values =
      child_environment({{"CODEX_HOME", (runtime / "home").string()}});
  std::vector<std::string> environment_storage;
  for (const auto& [name, value] : environment_values)
    environment_storage.push_back(name + "=" + value);
  std::vector<char*> arguments;
  for (auto& item : args) arguments.push_back(item.data());
  arguments.push_back(nullptr);
  std::vector<char*> environment_block;
  for (auto& item : environment_storage) environment_block.push_back(item.data());
  environment_block.push_back(nullptr);
  int input[2], output[2], errors[2];
  if (pipe(input) || pipe(output) || pipe(errors)) {
    fs::remove_all(runtime, copy_error);
    throw ProcessError("could not create Codex App Server pipes");
  }
  static std::once_flag sigpipe_once;
  std::call_once(sigpipe_once, [] { std::signal(SIGPIPE, SIG_IGN); });
  const auto child = fork();
  if (child == 0) {
    setsid(); chdir(runtime.c_str());
    dup2(input[0], STDIN_FILENO); dup2(output[1], STDOUT_FILENO);
    dup2(errors[1], STDERR_FILENO);
    close(input[0]); close(input[1]); close(output[0]); close(output[1]);
    close(errors[0]); close(errors[1]);
    execve(executable->c_str(), arguments.data(), environment_block.data()); _exit(127);
  }
  close(input[0]); close(output[1]); close(errors[1]);
  if (child < 0) {
    close(input[1]); close(output[0]); close(errors[0]);
    fs::remove_all(runtime, copy_error);
    throw ProcessError("could not start Codex App Server");
  }
  pid = child; stdin_fd = input[1]; stdout_fd = output[0]; stderr_fd = errors[0];
  failed = false; stopping = false;
  reader = std::thread([this] { reader_loop(); });
  stderr_reader = std::thread([this] {
    std::array<char, 16'384> buffer{}; std::size_t total = 0; ssize_t count;
    while ((count = ::read(stderr_fd, buffer.data(), buffer.size())) > 0) {
      total += static_cast<std::size_t>(count);
      if (total > max_output) {
        { std::lock_guard lock(state_mutex); failed = true;
          failure = "provider output exceeded configured limit"; }
        state_changed.notify_all(); kill(-pid, SIGKILL); break;
      }
    }
  });
  try {
    rpc("initialize", {{"clientInfo", {{"name", "kessel_cpp"},
                                        {"title", "Kessel C++"},
                                        {"version", KESSEL_CPP_VERSION}}}});
    write({{"method", "initialized"}, {"params", json::object()}});
  } catch (...) { stop(); throw; }
}

void CodexProvider::WarmServer::write(const json& payload) {
  auto text = json_compact(payload) + "\n"; std::lock_guard lock(write_mutex);
  std::size_t offset = 0;
  while (offset < text.size()) {
    const auto count = ::write(stdin_fd, text.data() + offset, text.size() - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) throw ProcessError("Codex App Server closed stdin");
    offset += static_cast<std::size_t>(count);
  }
}

void CodexProvider::WarmServer::reader_loop() {
  std::array<char, 16'384> buffer{}; std::string pending; ssize_t count;
  while ((count = ::read(stdout_fd, buffer.data(), buffer.size())) > 0) {
    pending.append(buffer.data(), static_cast<std::size_t>(count));
    std::size_t newline;
    while ((newline = pending.find('\n')) != std::string::npos) {
      auto line = pending.substr(0, newline); pending.erase(0, newline + 1);
      try {
        auto message = json::parse(line); std::lock_guard lock(state_mutex);
        if (message.contains("id") && message["id"].is_number_integer()) {
          const int id = message["id"].get<int>(); responses[id] = std::move(message);
        } else notifications.push_back(std::move(message));
        state_changed.notify_all();
      } catch (const std::exception& error) {
        std::cerr << "Codex App Server emitted invalid JSON: " << error.what() << '\n';
      }
    }
  }
  if (!stopping) {
    std::lock_guard lock(state_mutex); failed = true;
    failure = "Codex App Server exited unexpectedly"; state_changed.notify_all();
  }
}

void CodexProvider::WarmServer::stop() {
  stopping = true;
  if (stdin_fd >= 0) { close(stdin_fd); stdin_fd = -1; }
  if (pid > 0) {
    int status = 0; bool exited = false;
    for (int attempt = 0; attempt < 100; ++attempt) {
      const auto result = waitpid(pid, &status, WNOHANG);
      if (result == pid || (result < 0 && errno == ECHILD)) { exited = true; break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (!exited) { kill(-pid, SIGKILL); while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {} }
  }
  if (reader.joinable()) reader.join();
  if (stderr_reader.joinable()) stderr_reader.join();
  if (stdout_fd >= 0) close(stdout_fd);
  if (stderr_fd >= 0) close(stderr_fd);
  stdout_fd = stderr_fd = -1; pid = -1;
  std::error_code error; if (!runtime.empty()) fs::remove_all(runtime, error);
  runtime.clear();
}
#endif

json CodexProvider::WarmServer::rpc(const std::string& method, const json& params) {
  int id;
  { std::lock_guard lock(state_mutex); id = next_id++; }
  write({{"method", method}, {"id", id}, {"params", params}});
  std::unique_lock lock(state_mutex); const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout);
  if (!state_changed.wait_until(lock, deadline, [&] { return responses.contains(id) || failed; })) throw ProcessError("Codex App Server did not answer " + method, ProcessError::Kind::timeout);
  if (failed) throw ProcessError(failure);
  auto response = std::move(responses[id]); responses.erase(id); lock.unlock();
  if (response.contains("error"))
    throw ProcessError(response["error"].value("message", method + " failed"));
  return response.value("result", json::object());
}

std::optional<json> CodexProvider::WarmServer::next_notification(const std::string& thread_id, std::chrono::steady_clock::time_point deadline) {
  std::unique_lock lock(state_mutex);
  auto find = [&]() -> std::deque<json>::iterator { return std::find_if(notifications.begin(), notifications.end(), [&](const json& message) { auto params = message.value("params", json::object()); return params.value("threadId", "") == thread_id || message.value("method", "") == "server/error"; }); };
  if (!state_changed.wait_until(lock, deadline, [&] { return find() != notifications.end() || failed; })) return std::nullopt;
  if (failed) throw ProcessError(failure);
  auto iterator = find();
  if (iterator == notifications.end()) return std::nullopt;
  auto result = std::move(*iterator); notifications.erase(iterator); return result;
}

ProviderResult CodexProvider::WarmServer::complete(const ChatRequest& request, const LineCallback& delta) {
  start();
  json thread_params = {{"cwd", runtime.string()}, {"approvalPolicy", "never"}, {"sandbox", "read-only"}, {"ephemeral", true}, {"baseInstructions", read_file(asset_root() / "resources" / "codex_instructions.txt")}, {"serviceName", "kessel"}, {"serviceTier", request.service_tier}, {"config", {{"skills", {{"max_context_tokens", 1}}}, {"agents", {{"enabled", false}}}, {"mcp_servers", json::object()}}}};
  if (!default_model("codex", request.model)) thread_params["model"] = request.model;
  auto thread_response = rpc("thread/start", thread_params); auto thread_id = thread_response["thread"].value("id", ""); if (thread_id.empty()) throw ProcessError("Codex App Server returned no thread id");
  json turn_params = {{"threadId", thread_id}, {"input", json::array({{{"type", "text"}, {"text", build_prompt(request)}}})}, {"effort", request.reasoning_effort}, {"summary", "none"}, {"serviceTierForTurn", request.service_tier}};
  if (auto schema = output_schema(request)) turn_params["outputSchema"] = *schema;
  auto turn_response = rpc("turn/start", turn_params); auto turn_id = turn_response.value("turn", json::object()).value("id", "");
  std::string full_text; std::optional<Usage> usage; bool complete = false; const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout);
  try {
    while (!complete) { auto message = next_notification(thread_id, deadline); if (!message) throw ProcessError("provider exceeded timeout", ProcessError::Kind::timeout); auto method = message->value("method", ""); auto params = message->value("params", json::object());
      if (method == "item/agentMessage/delta") { auto text = params.value("delta", ""); full_text += text; if (full_text.size() > max_output) throw ProcessError("provider output exceeded configured limit", ProcessError::Kind::output_limit); if (!text.empty() && delta && !delta(text)) break; }
      else if (method == "item/completed") { auto item = params.value("item", json::object()); if (item.value("type", "") == "agentMessage" && full_text.empty()) { full_text = item.value("text", ""); if (delta && !full_text.empty()) delta(full_text); } }
      else if (method == "thread/tokenUsage/updated") { auto raw = params.value("tokenUsage", json::object()).value("last", json::object()); usage = Usage{raw.value("inputTokens", 0), raw.value("outputTokens", 0), raw.value("cachedInputTokens", 0)}; }
      else if (method == "turn/completed") { complete = true; auto turn = params.value("turn", json::object()); if (turn.value("status", "") == "failed") throw ProcessError(turn.value("error", json::object()).value("message", "Codex turn failed")); }
      else if (method == "error" || method == "server/error") throw ProcessError(params.value("error", json::object()).value("message", "Codex turn failed"));
    }
  } catch (...) { if (!turn_id.empty()) try { rpc("turn/interrupt", {{"threadId", thread_id}, {"turnId", turn_id}}); } catch (...) {} throw; }
  if (!complete && !turn_id.empty()) try { rpc("turn/interrupt", {{"threadId", thread_id}, {"turnId", turn_id}}); } catch (...) {}
  if (full_text.empty()) throw ProcessError("Codex completed without an assistant message");
  ProviderResult result; result.text = full_text; result.model = request.model;
  result.usage = usage; return result;
}

std::vector<std::string> CodexProvider::WarmServer::models() {
  std::lock_guard listing(models_mutex); start(); std::vector<std::string> result; std::optional<std::string> cursor;
  do { json params = {{"limit", 100}, {"includeHidden", false}}; if (cursor) params["cursor"] = *cursor; auto response = rpc("model/list", params); for (const auto& item : response.value("data", json::array())) { if (item.value("hidden", false)) continue; auto id = item.value("id", item.value("model", "")); if (!id.empty() && std::find(result.begin(), result.end(), id) == result.end()) result.push_back(id); } if (response.contains("nextCursor") && response["nextCursor"].is_string() && !response["nextCursor"].get<std::string>().empty()) cursor = response["nextCursor"]; else cursor.reset(); } while (cursor);
  return result;
}

ProviderResult CodexProvider::warm_complete(const ChatRequest& request, const LineCallback& delta) {
  { std::lock_guard lock(warm_mutex_); if (!warm_) warm_ = std::make_unique<WarmServer>(command_, timeout_, max_output_); }
  auto result = warm_->complete(request, delta); result = parse_structured_result(request, std::move(result)); observe_model(result.model); return result;
}
std::vector<std::string> CodexProvider::models() { std::lock_guard lock(warm_mutex_); if (!warm_) warm_ = std::make_unique<WarmServer>(command_, timeout_, max_output_); return warm_->models(); }
void CodexProvider::close() { std::lock_guard lock(warm_mutex_); if (warm_) warm_->stop(); warm_.reset(); }

std::unique_ptr<Provider> make_codex_provider(const Settings& settings) { return std::make_unique<CodexProvider>(settings); }
std::unique_ptr<Provider> make_claude_provider(const Settings& settings) { return std::make_unique<ClaudeProvider>(settings); }

}  // namespace kessel
