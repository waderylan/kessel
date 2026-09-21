#include "kessel/core.hpp"

#include <httplib.h>

#include <chrono>
#include <cerrno>
#include <iostream>
#include <sstream>

#ifdef _WIN32
#include <windows.h>
#else
#include <csignal>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace kessel {

static fs::path own_executable() {
#ifdef _WIN32
  std::vector<wchar_t> buffer(32768); const auto size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size())); return fs::path(std::wstring(buffer.data(), size));
#else
  return fs::canonical("/proc/self/exe");
#endif
}

static bool is_running(const UserConfig& config, int timeout_seconds = 1) {
  httplib::Client client(config.host == "::1" ? "::1" : config.host, config.port); client.set_connection_timeout(timeout_seconds); client.set_read_timeout(timeout_seconds);
  auto response = client.Get("/health", httplib::Headers{{"Host", (config.host == "::1" ? "[::1]" : config.host) + ":" + std::to_string(config.port)}});
  if (!response || response->status != 200) return false;
  try { auto body = json::parse(response->body); return body.value("status", "") == "ok" && body.value("providers", json::object()).contains("codex") && body["providers"].contains("claude"); } catch (...) { return false; }
}

static UserConfig configured() {
  auto config = UserConfig::load(); if (!config.api_key || config.api_key->empty()) throw Error("Kessel is not set up. Run: kessel-cpp setup", 1); return config;
}

static std::string shell_quote(const std::string& value, const std::string& shell) {
  if (shell == "powershell") { auto copy = value; std::size_t position = 0; while ((position = copy.find('\'', position)) != std::string::npos) { copy.insert(position, 1, '\''); position += 2; } return "'" + copy + "'"; }
  if (shell == "fish") { auto copy = value; std::size_t position = 0; while ((position = copy.find('\'', position)) != std::string::npos) { copy.replace(position, 1, "\\'"); position += 2; } return "'" + copy + "'"; }
  std::string result = "'"; for (const auto c : value) result += c == '\'' ? "'\\''" : std::string(1, c); return result + "'";
}

static std::string render_env(const UserConfig& config, const std::string& provider, const std::string& shell) {
  const std::vector<std::pair<std::string, std::string>> values = {{"OPENAI_BASE_URL", config.base_url() + "/v1/" + provider}, {"OPENAI_API_KEY", config.api_key.value_or("")}, {"ANTHROPIC_BASE_URL", config.base_url()}, {"ANTHROPIC_API_KEY", config.api_key.value_or("")}};
  std::ostringstream output; for (std::size_t index = 0; index < values.size(); ++index) { const auto& [name, value] = values[index]; if (index) output << '\n'; if (shell == "powershell") output << "$env:" << name << " = " << shell_quote(value, shell); else if (shell == "fish") output << "set -gx " << name << ' ' << shell_quote(value, shell) << ';'; else output << "export " << name << '=' << shell_quote(value, shell); } return output.str();
}

static std::string render_connect(const UserConfig& config, const std::string& target) {
  const auto key = config.api_key.value_or(""); const auto openai = config.base_url() + "/v1/claude"; const auto root = config.base_url();
  if (target == "openai-python") return "Paste into your Python code:\n\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url=\"" + openai + "\",\n    api_key=\"" + key + "\",\n)";
  if (target == "openai-node") return "Paste into your Node.js code:\n\nimport OpenAI from \"openai\";\n\nconst client = new OpenAI({ baseURL: \"" + openai + "\", apiKey: \"" + key + "\" });";
  if (target == "anthropic-python") return "Paste into your Python code:\n\nfrom anthropic import Anthropic\n\nclient = Anthropic(\n    base_url=\"" + root + "\",\n    api_key=\"" + key + "\",\n)";
  if (target == "anthropic-node") return "Paste into your Node.js code:\n\nimport Anthropic from \"@anthropic-ai/sdk\";\n\nconst client = new Anthropic({ baseURL: \"" + root + "\", apiKey: \"" + key + "\" });";
  if (target == "cursor") return "Paste these values in Cursor Settings > Models > Add Custom Model:\n\nModel name: default\nOverride OpenAI Base URL: " + openai + "\nOpenAI API Key: " + key;
  if (target == "continue") return "Paste into ~/.continue/config.yaml:\n\nname: Kessel\nversion: 0.0.1\nschema: v1\nmodels:\n  - name: Kessel Claude\n    provider: openai\n    model: default\n    apiBase: " + openai + "\n    apiKey: " + key;
#ifdef _WIN32
  if (target == "curl") return "Paste into PowerShell:\n\ncurl.exe \"" + openai + "/chat/completions\" `\n  -H \"Authorization: Bearer " + key + "\" `\n  -H \"Content-Type: application/json\" `\n  -d '{\"model\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}'";
  if (target == "aider") return "Paste into PowerShell before running Aider:\n\n$env:OPENAI_API_BASE = \"" + openai + "\"\n$env:OPENAI_API_KEY = \"" + key + "\"\naider --model openai/default";
#else
  if (target == "curl") return "Paste into a terminal:\n\ncurl " + openai + "/chat/completions -H 'Authorization: Bearer " + key + "' -H 'Content-Type: application/json' -d '{\"model\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}'";
  if (target == "aider") return "Paste into a terminal before running Aider:\n\nexport OPENAI_API_BASE=" + openai + "\nexport OPENAI_API_KEY=" + key + "\naider --model openai/default";
#endif
  return "Paste these values into " + target + ":\n\nOpenAI-compatible base URL: " + openai + "\nAPI key: " + key + "\nAuthentication header: Authorization: Bearer <API key>";
}

struct Health { std::string name, display, command, version; bool installed = false, authenticated = false; };

static Health check_provider(std::string name, std::string display, std::string command,
                             std::vector<std::string> auth) {
  Health result; result.name = std::move(name); result.display = std::move(display);
  result.command = std::move(command); auto executable = find_executable(result.command); if (!executable) return result; result.installed = true; result.command = executable->string();
  try { auto version = run_process({result.command, "--version"}, "", fs::current_path(), 15, 65'536); result.version = trim(version.out + version.err); auth.insert(auth.begin(), result.command); run_process(auth, "", fs::current_path(), 15, 65'536); result.authenticated = true; } catch (...) {}
  return result;
}

static std::vector<Health> check_providers() {
  auto config = UserConfig::load(); return {check_provider("claude", "Claude Code", config.claude_command.value_or("claude"), {"auth", "status"}), check_provider("codex", "Codex", config.codex_command.value_or("codex"), {"login", "status"})};
}

static void print_doctor(const std::vector<Health>& checks) {
  for (const auto& check : checks) { if (check.installed && check.authenticated) std::cout << "[ok] " << check.display << " is installed and logged in" << (check.version.empty() ? "" : " (" + check.version + ")") << '\n'; else if (!check.installed) std::cout << "[fix] " << check.display << " is not installed.\n      Run: " << (check.name == "codex" ? "npm install -g @openai/codex" : "npm install -g @anthropic-ai/claude-code") << '\n'; else std::cout << "[fix] " << check.display << " isn't logged in.\n      Run: " << check.name << " login\n"; }
}

#ifdef _WIN32
static std::wstring wide(std::string_view text) { int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0); std::wstring result(size, L'\0'); MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size); return result; }
static std::string narrow(std::wstring_view text) { int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr); std::string result(size, '\0'); WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size, nullptr, nullptr); return result; }
static std::wstring quote(std::string_view value) {
  auto input = wide(value); if (input.find_first_of(L" \t\n\v\"") == std::wstring::npos) return input;
  std::wstring result = L"\""; std::size_t slashes = 0;
  for (auto character : input) {
    if (character == L'\\') { ++slashes; continue; }
    if (character == L'\"') { result.append(slashes * 2 + 1, L'\\'); result += character; slashes = 0; continue; }
    result.append(slashes, L'\\'); slashes = 0; result += character;
  }
  result.append(slashes * 2, L'\\'); return result + L"\"";
}
struct CaseInsensitiveLess {
  bool operator()(const std::string& left, const std::string& right) const {
    return _stricmp(left.c_str(), right.c_str()) < 0;
  }
};
static std::vector<wchar_t> inherited_environment_block(
    const std::map<std::string, std::string>& additions) {
  std::map<std::string, std::string, CaseInsensitiveLess> values;
  if (auto block = GetEnvironmentStringsW()) {
    for (const wchar_t* entry = block; *entry; entry += std::wcslen(entry) + 1) {
      std::wstring item(entry); const auto equals = item.find(L'=');
      if (equals == std::wstring::npos || equals == 0) continue;
      values[narrow(std::wstring_view(item).substr(0, equals))] =
          narrow(std::wstring_view(item).substr(equals + 1));
    }
    FreeEnvironmentStringsW(block);
  }
  for (const auto& [name, value] : additions) values[name] = value;
  std::vector<wchar_t> result;
  for (const auto& [name, value] : values) {
    auto entry = wide(name + "=" + value);
    result.insert(result.end(), entry.begin(), entry.end()); result.push_back(L'\0');
  }
  result.push_back(L'\0'); return result;
}
static PROCESS_INFORMATION spawn(const std::vector<std::string>& command, bool detached,
                                 const std::map<std::string, std::string>& additions = {}) {
  std::wstring line; for (std::size_t i = 0; i < command.size(); ++i) { if (i) line += L' '; line += quote(command[i]); }
  auto environment = inherited_environment_block(additions);
  STARTUPINFOW startup{}; startup.cb = sizeof(startup); PROCESS_INFORMATION process{}; DWORD flags = CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT | (detached ? DETACHED_PROCESS | CREATE_NO_WINDOW : 0);
  if (!CreateProcessW(wide(command[0]).c_str(), line.data(), nullptr, nullptr, FALSE, flags, environment.data(), nullptr, &startup, &process)) throw Error("Application command not found: " + command[0], 1);
  return process;
}
static HANDLE attach_kill_job(HANDLE process) {
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (!job) return nullptr;
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{};
  info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info, sizeof(info)) ||
      !AssignProcessToJobObject(job, process)) {
    CloseHandle(job); return nullptr;
  }
  return job;
}
static void register_startup() {
  HKEY key; if (RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, nullptr, 0, KEY_SET_VALUE, nullptr, &key, nullptr) != ERROR_SUCCESS) throw Error("Could not register Kessel for the current user", 1);
  auto value = L"\"" + own_executable().wstring() + L"\" serve"; RegSetValueExW(key, L"Kessel", 0, REG_SZ, reinterpret_cast<const BYTE*>(value.c_str()), static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t))); RegCloseKey(key);
}
#endif

static void request_stop() { fs::create_directories(state_directory()); std::ofstream(state_directory() / "stop.request") << "stop\n"; }

static int command_start() {
  auto config = UserConfig::load().with_generated_key(); config.save(); if (is_running(config)) { std::cout << "Durable Kessel is already running.\n"; return 0; }
#ifdef _WIN32
  register_startup(); auto process = spawn({own_executable().string(), "serve"}, true); CloseHandle(process.hThread); CloseHandle(process.hProcess);
#else
  const auto unit_dir = fs::path(environment("HOME")) / ".config/systemd/user"; fs::create_directories(unit_dir); const auto executable = own_executable().string(); write_file_atomic(unit_dir / "kessel.service", "[Unit]\nDescription=Kessel local API\nAfter=network.target\n\n[Service]\nExecStart=\"" + executable + "\" serve\nRestart=on-failure\nRestartSec=2\n\n[Install]\nWantedBy=default.target\n"); run_process({"systemctl", "--user", "daemon-reload"}, "", fs::current_path(), 30, 65'536); run_process({"systemctl", "--user", "enable", "--now", "kessel.service"}, "", fs::current_path(), 30, 65'536);
#endif
  for (int count = 0; count < 100 && !is_running(config); ++count)
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
  if (!is_running(config)) throw Error("the service did not become ready within 20 seconds", 1);
  std::cout << "Durable Kessel started.\n"; return 0;
}

static std::pair<bool, std::string> test_provider_request(
    const UserConfig& config, const std::string& provider) {
  httplib::Client client(config.host == "::1" ? "::1" : config.host, config.port);
  client.set_connection_timeout(5); client.set_read_timeout(90);
  const auto body = json_compact({{"model", "default"}, {"messages", json::array({{{"role", "user"}, {"content", "Reply with OK."}}})}, {"max_tokens", 8}});
  auto response = client.Post("/v1/" + provider + "/chat/completions",
      httplib::Headers{{"Host", (config.host == "::1" ? "[::1]" : config.host) + ":" + std::to_string(config.port)},
                       {"Authorization", "Bearer " + config.api_key.value_or("")}},
      body, "application/json");
  if (!response) return {false, "could not connect to the local service"};
  try {
    auto payload = json::parse(response->body);
    if (response->status == 200)
      return {true, payload["choices"][0]["message"].value("content", "")};
    return {false, payload.value("error", json::object()).value("message", response->body)};
  } catch (...) { return {false, response->body}; }
}

int self_test() {
  int failures = 0; auto check = [&](bool condition, std::string_view name) { if (!condition) { std::cerr << "[fail] " << name << '\n'; ++failures; } };
  auto request = parse_chat_request({{"model", "default"}, {"messages", json::array({{{"role", "system"}, {"content", "Be concise."}}, {{"role", "user"}, {"content", "Hello"}}})}}); check(build_prompt(request).find("[SYSTEM]") < build_prompt(request).find("[USER]"), "prompt roles"); check(estimate_tokens("alpha beta gamma") == 3, "token estimate"); request.stop = {"STOP"}; ProviderResult value; value.text = "before STOP after"; value.model = "default"; value = apply_output_controls(request, value); check(value.text == "before " && value.finish_reason == "stop", "stop control");
  check(estimate_tokens("hello, world!") == 4, "punctuation token parity");
  check(estimate_tokens("こんにちは世界") == 2, "Unicode token parity");
  check(token_prefix(" before STOP after", 2) == " before STOP", "token prefix parity");
  json schema = {{"type", "object"}, {"properties", {{"city", {{"type", "string"}}}}}, {"required", json::array({"city"})}};
  json tool = {{"type", "function"}, {"function", {{"name", "weather"}, {"parameters", schema}}}};
  json structured_body = {{"model", "default"}, {"messages", json::array({{{"role", "user"}, {"content", "weather"}}})}, {"tools", json::array({tool})}, {"tool_choice", "required"}};
  auto structured = parse_chat_request(structured_body);
  ProviderResult structured_result; structured_result.text = R"({"name":"weather","arguments":{"city":"Seattle"}})"; structured_result.model = "default";
  auto parsed = parse_structured_result(structured, std::move(structured_result));
  check(!parsed.text && parsed.tool_calls.size() == 1, "tool mapping");
  json constrained = {{"type", "object"}, {"properties", {{"code", {{"type", "string"}, {"minLength", 3}, {"pattern", "^[A-Z]+$"}}}}}, {"required", json::array({"code"})}, {"additionalProperties", false}};
  check(validate_json_value(constrained, {{"code", "ABC"}}), "JSON schema constraints accept");
  check(!validate_json_value(constrained, {{"code", "a"}}), "JSON schema constraints reject");
  check(validate_json_value({{"oneOf", json::array({json{{"type", "string"}}, json{{"type", "number"}}})}}, 4), "JSON schema composition");
  if (!failures) std::cout << "[ok] Kessel C++ self-tests passed\n";
  return failures ? 1 : 0;
}

static void usage(std::ostream& output = std::cerr) {
  output << "usage: kessel-cpp {setup|doctor|env|connect|key|start|stop|status|run|serve} ...\n\n"
         << "Local API for Codex and Claude Code subscriptions\n\n"
         << "commands:\n"
         << "  setup    check providers, configure Kessel, and test requests\n"
         << "  doctor   check provider installation and login\n"
         << "  env      print client environment variables\n"
         << "  connect  print setup for a client\n"
         << "  key      manage the local API key\n"
         << "  start    install and start durable Kessel\n"
         << "  stop     stop the current Kessel server\n"
         << "  status   show whether Kessel is running\n"
         << "  run      run Kessel for an application or terminal\n"
         << "  serve    run the foreground API server\n";
}

int run_cli(int argc, char** argv) {
  if (argc < 2) { usage(); return 2; }
  const std::string command = argv[1];
  if (command == "-h" || command == "--help") { usage(std::cout); return 0; }
  if (argc >= 3 && (std::string(argv[2]) == "-h" || std::string(argv[2]) == "--help")) {
    std::cout << "usage: kessel-cpp " << command << " [options]\n"; return 0;
  }
  if (command == "self-test") return self_test();
  if (command == "serve") { auto config = configured(); std::error_code error; fs::remove(state_directory() / "stop.request", error); return run_server(Settings::load()); }
  if (command == "doctor") { auto checks = check_providers(); print_doctor(checks); return std::any_of(checks.begin(), checks.end(), [](const auto& item) { return item.installed && item.authenticated; }) ? 0 : 1; }
  if (command == "setup") {
    std::cout << "Checking providers...\n"; auto checks = check_providers(); print_doctor(checks);
    std::vector<const Health*> working;
    for (const auto& check : checks)
      if (check.installed && check.authenticated) working.push_back(&check);
    if (working.empty()) {
      std::cerr << "[error] Kessel needs at least one installed and logged-in provider.\n"
                << "Install or log in to Codex or Claude Code using the guidance above, "
                   "then run `kessel-cpp setup` again.\n";
      return 1;
    }
    if (working.size() == 1)
      std::cout << "[ok] " << working.front()->display
                << " is ready. Kessel will use that provider; the other provider is optional.\n";
    else
      std::cout << "[ok] Codex and Claude Code are both ready.\n";
    auto old = UserConfig::load(); const bool created = !old.api_key || old.api_key->empty();
    auto config = old.with_generated_key();
    for (const auto* check : working) {
      if (check->name == "codex") config.codex_command = check->command;
      if (check->name == "claude") config.claude_command = check->command;
    }
    config.save();
    std::cout << (created ? "[ok] Generated a local API key\n" : "[ok] API key already exists\n");
    bool owned = false; bool tests_failed = false;
    if (!is_running(config)) {
#ifdef _WIN32
      auto process = spawn({own_executable().string(), "serve"}, true);
      CloseHandle(process.hThread); CloseHandle(process.hProcess);
#else
      const auto child = fork();
      if (child == 0) { setsid(); execl(own_executable().c_str(), own_executable().c_str(), "serve", nullptr); _exit(127); }
      if (child < 0) throw Error("Could not start temporary Kessel", 1);
#endif
      owned = true;
      for (int count = 0; count < 100 && !is_running(config); ++count)
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
      if (!is_running(config)) throw Error("temporary server did not become ready within 20 seconds", 1);
      std::cout << "[ok] Temporary Kessel started for provider tests\n";
    } else std::cout << "[ok] Existing Kessel server detected; using it for provider tests\n";
    for (const auto* check : working) {
      const auto [success, detail] = test_provider_request(config, check->name);
      std::cout << (success ? "[ok] " : "[error] ") << check->display
                << " test request " << (success ? "succeeded: " : "failed: ")
                << detail << '\n';
      tests_failed |= !success;
    }
    if (owned) {
      request_stop();
      for (int count = 0; count < 75 && is_running(config); ++count)
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
      std::cout << "[ok] Temporary Kessel stopped\n";
    }
    std::cout << "\nConnection details\n";
    const auto has_provider = [&](std::string_view name) {
      return std::any_of(working.begin(), working.end(), [&](const auto* item) {
        return item->name == name;
      });
    };
    if (has_provider("claude"))
      std::cout << "OpenAI base URL (Claude): " << config.base_url()
                << "/v1/claude\nAnthropic base URL:       " << config.base_url() << '\n';
    if (has_provider("codex"))
      std::cout << "OpenAI base URL (Codex):  " << config.base_url() << "/v1/codex\n";
    std::cout << "API key:                  run `kessel-cpp key` to reveal it\n\n"
              << "Run an application with managed settings:\n"
              << "kessel-cpp run --provider " << working.front()->name
              << " -- your_app\nSetup does not leave Kessel running in the background.\n";
    return tests_failed ? 1 : 0;
  }
  if (command == "env") { std::string provider = "claude", shell = "posix"; for (int i = 2; i < argc; ++i) { if (std::string(argv[i]) == "--provider" && i + 1 < argc) provider = argv[++i]; else if (std::string(argv[i]) == "--shell" && i + 1 < argc) shell = argv[++i]; } if (provider != "codex" && provider != "claude") throw Error("invalid provider", 2); std::cout << render_env(configured(), provider, shell) << '\n'; return 0; }
  if (command == "connect") { if (argc < 3) throw Error("connect requires a target", 2); std::cout << render_connect(configured(), argv[2]) << '\n'; return 0; }
  if (command == "key") { auto config = UserConfig::load().with_generated_key(); bool rotate = false, copy = false; for (int i = 2; i < argc; ++i) { rotate |= std::string(argv[i]) == "--rotate"; copy |= std::string(argv[i]) == "--copy"; } if (rotate) { if (!environment("KESSEL_API_KEY").empty()) throw Error("Cannot rotate while KESSEL_API_KEY overrides the saved key", 1); config = config.with_rotated_key(); config.save(); std::cout << "Kessel API key rotated. Existing clients must use the new key.\n"; } else config.save(); if (copy) { run_process({"clip"}, config.api_key.value_or(""), fs::current_path(), 5, 1024); std::cout << "Kessel API key copied to the clipboard.\n"; } else if (!rotate) std::cout << config.api_key.value_or("") << '\n'; return 0; }
  if (command == "start") return command_start();
  if (command == "status") { auto config = configured(); if (is_running(config)) { std::cout << "Kessel is running at " << config.base_url() << ".\n"; return 0; } std::cerr << "Kessel isn't running. Use: kessel-cpp run --provider codex or kessel-cpp start\n"; return 1; }
  if (command == "stop") { auto config = configured(); if (is_running(config)) { request_stop(); for (int count = 0; count < 75 && is_running(config); ++count) std::this_thread::sleep_for(std::chrono::milliseconds(200)); if (is_running(config)) throw Error("Kessel did not stop within 15 seconds", 1); } std::cout << "Kessel stopped.\n"; return 0; }
  if (command == "run") { auto config = configured(); std::string provider; int application_index = argc; for (int i = 2; i < argc; ++i) { if (std::string(argv[i]) == "--provider" && i + 1 < argc) provider = argv[++i]; else if (std::string(argv[i]) == "--") { application_index = i + 1; break; } else if (application_index == argc && provider.size()) { application_index = i; break; } } if (provider != "codex" && provider != "claude") throw Error("run requires --provider codex|claude", 2); bool owned = false;
#ifdef _WIN32
    HANDLE owned_process = nullptr; HANDLE owned_job = nullptr;
#endif
    if (!is_running(config)) {
#ifdef _WIN32
      auto process = spawn({own_executable().string(), "serve"}, true);
      owned_process = process.hProcess; owned_job = attach_kill_job(process.hProcess);
      CloseHandle(process.hThread);
#else
      if (fork() == 0) { execl(own_executable().c_str(), own_executable().c_str(), "serve", nullptr); _exit(127); }
#endif
      owned = true; for (int count = 0; count < 100 && !is_running(config); ++count) std::this_thread::sleep_for(std::chrono::milliseconds(200)); if (!is_running(config)) throw Error("temporary server did not become ready", 1); std::cout << "Temporary Kessel started at " << config.base_url() << "; client provider: " << provider << ".\n";
    } else std::cout << "Existing Kessel server detected at " << config.base_url() << "; reusing it.\n";
    if (application_index >= argc) {
      if (!owned) { std::cout << "No application command was supplied; Kessel remains owned elsewhere.\n"; return 0; }
      std::cout << "Keep this terminal open. Press Ctrl+C to stop Kessel.\n";
      while (is_running(config)) std::this_thread::sleep_for(std::chrono::seconds(1));
#ifdef _WIN32
      if (owned_process) CloseHandle(owned_process);
      if (owned_job) CloseHandle(owned_job);
#endif
      return 0;
    }
    std::vector<std::string> application; for (int i = application_index; i < argc; ++i) application.push_back(argv[i]); int code = 1;
#ifdef _WIN32
    auto process = spawn(application, false, {{"OPENAI_BASE_URL", config.base_url() + "/v1/" + provider}, {"OPENAI_API_KEY", config.api_key.value_or("")}, {"ANTHROPIC_BASE_URL", config.base_url()}, {"ANTHROPIC_API_KEY", config.api_key.value_or("")}});
    auto application_job = attach_kill_job(process.hProcess); bool runtime_stopped = false;
    while (WaitForSingleObject(process.hProcess, 500) == WAIT_TIMEOUT) {
      if (!is_running(config)) { runtime_stopped = true; if (application_job) TerminateJobObject(application_job, 1); else TerminateProcess(process.hProcess, 1); break; }
    }
    WaitForSingleObject(process.hProcess, INFINITE); DWORD result; GetExitCodeProcess(process.hProcess, &result);
    code = runtime_stopped ? 1 : static_cast<int>(result);
    if (runtime_stopped) std::cerr << "Kessel stopped; ending the managed application.\n";
    CloseHandle(process.hThread); CloseHandle(process.hProcess); if (application_job) CloseHandle(application_job);
#else
    std::vector<char*> arguments;
    for (auto& item : application) arguments.push_back(item.data());
    arguments.push_back(nullptr);
    const auto child = fork();
    if (child < 0) throw Error("Could not start application", 1);
    if (child == 0) {
      setsid();
      setenv("OPENAI_BASE_URL", (config.base_url() + "/v1/" + provider).c_str(), 1);
      setenv("OPENAI_API_KEY", config.api_key.value_or("").c_str(), 1);
      setenv("ANTHROPIC_BASE_URL", config.base_url().c_str(), 1);
      setenv("ANTHROPIC_API_KEY", config.api_key.value_or("").c_str(), 1);
      execvp(arguments[0], arguments.data()); _exit(127);
    }
    int status = 0; bool runtime_stopped = false;
    while (true) {
      const auto result = waitpid(child, &status, WNOHANG);
      if (result == child) break;
      if (result < 0 && errno != EINTR) break;
      if (!is_running(config)) { runtime_stopped = true; kill(-child, SIGTERM); break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    if (runtime_stopped) {
      for (int count = 0; count < 50 && waitpid(child, &status, WNOHANG) == 0; ++count)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      if (waitpid(child, &status, WNOHANG) == 0) { kill(-child, SIGKILL); while (waitpid(child, &status, 0) < 0 && errno == EINTR) {} }
      std::cerr << "Kessel stopped; ending the managed application.\n";
    }
    code = runtime_stopped ? 1 : WIFEXITED(status) ? WEXITSTATUS(status) : 1;
#endif
    if (owned) { request_stop(); for (int count = 0; count < 50 && is_running(config); ++count) std::this_thread::sleep_for(std::chrono::milliseconds(200)); }
#ifdef _WIN32
    if (owned_process) CloseHandle(owned_process);
    if (owned_job) CloseHandle(owned_job);
#endif
    return code;
  }
  usage(); return 2;
}

}  // namespace kessel
