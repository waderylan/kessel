#include "kessel/core.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <random>
#include <regex>
#include <set>
#include <sstream>

#include <encoding.h>

#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace kessel {

std::string environment(std::string_view name, std::string fallback) {
  std::string key(name);
  if (const char* value = std::getenv(key.c_str()); value != nullptr) return value;
  return fallback;
}

std::string trim(std::string value) {
  const auto first = value.find_first_not_of(" \t\r\n");
  if (first == std::string::npos) return {};
  const auto last = value.find_last_not_of(" \t\r\n");
  return value.substr(first, last - first + 1);
}

std::string lower(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return value;
}

std::vector<std::string> split(std::string_view input, char delimiter) {
  std::vector<std::string> result;
  std::size_t start = 0;
  while (start <= input.size()) {
    const auto end = input.find(delimiter, start);
    result.emplace_back(input.substr(start, end == std::string_view::npos
                                               ? input.size() - start
                                               : end - start));
    if (end == std::string_view::npos) break;
    start = end + 1;
  }
  return result;
}

bool environment_bool(std::string_view name, bool fallback) {
  const auto raw = environment(name);
  if (raw.empty()) return fallback;
  const auto value = lower(trim(raw));
  if (value == "1" || value == "true" || value == "yes" || value == "on") return true;
  if (value == "0" || value == "false" || value == "no" || value == "off") return false;
  throw Error(std::string(name) + " must be a boolean", 1, "configuration_error");
}

int environment_positive_int(std::string_view name, int fallback) {
  const auto raw = environment(name);
  if (raw.empty()) return fallback;
  try {
    std::size_t consumed = 0;
    const int value = std::stoi(raw, &consumed);
    if (consumed != raw.size() || value <= 0) throw std::invalid_argument("range");
    return value;
  } catch (...) {
    throw Error(std::string(name) + " must be a positive integer", 1,
                "configuration_error");
  }
}

fs::path config_directory() {
  if (const auto value = environment("KESSEL_CONFIG_DIR"); !value.empty()) return value;
#ifdef _WIN32
  auto root = environment("LOCALAPPDATA");
  if (root.empty()) root = environment("USERPROFILE") + "\\AppData\\Local";
  return fs::path(root) / "Kessel";
#else
  auto root = environment("XDG_CONFIG_HOME");
  if (root.empty()) root = environment("HOME") + "/.config";
  return fs::path(root) / "kessel";
#endif
}

fs::path state_directory() {
  if (const auto value = environment("KESSEL_STATE_DIR"); !value.empty()) return value;
#ifdef _WIN32
  return config_directory();
#else
  auto root = environment("XDG_STATE_HOME");
  if (root.empty()) root = environment("HOME") + "/.local/state";
  return fs::path(root) / "kessel";
#endif
}

fs::path asset_root() {
  if (const auto configured = environment("KESSEL_ASSET_DIR"); !configured.empty())
    return configured;
#ifdef _WIN32
  std::vector<wchar_t> buffer(32768);
  const auto size = GetModuleFileNameW(nullptr, buffer.data(),
                                       static_cast<DWORD>(buffer.size()));
  if (size > 0 && size < buffer.size()) {
    const auto adjacent = fs::path(std::wstring(buffer.data(), size)).parent_path();
    if (fs::is_directory(adjacent / "static") &&
        fs::is_directory(adjacent / "resources")) return adjacent;
  }
#else
  std::error_code error;
  const auto executable = fs::read_symlink("/proc/self/exe", error);
  if (!error) {
    const auto adjacent = executable.parent_path();
    if (fs::is_directory(adjacent / "static") &&
        fs::is_directory(adjacent / "resources")) return adjacent;
  }
#endif
  return fs::path(KESSEL_CPP_SOURCE_DIR);
}

std::string read_file(const fs::path& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw Error("Could not read " + path.string(), 1, "file_error");
  return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void write_file_atomic(const fs::path& path, std::string_view value) {
  fs::create_directories(path.parent_path());
#ifndef _WIN32
  ::chmod(path.parent_path().c_str(), 0700);
#endif
  const auto temporary = path.parent_path() /
      ("." + path.filename().string() + "." + random_hex(8) + ".tmp");
  {
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) throw Error("Could not write " + temporary.string(), 1, "file_error");
    output.write(value.data(), static_cast<std::streamsize>(value.size()));
    output.flush();
    if (!output) throw Error("Could not write " + temporary.string(), 1, "file_error");
  }
#ifndef _WIN32
  ::chmod(temporary.c_str(), 0600);
#endif
#ifdef _WIN32
  if (!MoveFileExW(temporary.wstring().c_str(), path.wstring().c_str(),
                   MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
    const auto code = GetLastError();
    fs::remove(temporary);
    throw Error("Could not replace " + path.string() + ": Windows error " +
                    std::to_string(code),
                1,
                "file_error");
  }
#else
  std::error_code error;
  fs::rename(temporary, path, error);
  if (error) {
    fs::remove(temporary);
    throw Error("Could not replace " + path.string() + ": " + error.message(), 1,
                "file_error");
  }
#endif
#ifndef _WIN32
  ::chmod(path.c_str(), 0600);
#endif
}

std::string random_hex(std::size_t bytes) {
  std::vector<unsigned char> data(bytes);
#ifdef _WIN32
  if (BCryptGenRandom(nullptr, data.data(), static_cast<ULONG>(data.size()),
                      BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
    throw Error("Secure random generation failed", 1, "random_error");
#else
  std::random_device random;
  for (auto& byte : data) byte = static_cast<unsigned char>(random());
#endif
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (const auto byte : data) output << std::setw(2) << static_cast<int>(byte);
  return output.str();
}

std::string random_key() { return "kessel_" + random_hex(32); }
std::string request_id(std::string_view prefix) { return std::string(prefix) + random_hex(16); }

bool constant_time_equal(std::string_view left, std::string_view right) {
  std::size_t difference = left.size() ^ right.size();
  const auto length = std::max(left.size(), right.size());
  for (std::size_t index = 0; index < length; ++index) {
    const unsigned char a = index < left.size() ? left[index] : 0;
    const unsigned char b = index < right.size() ? right[index] : 0;
    difference |= a ^ b;
  }
  return difference == 0;
}

std::string json_compact(const json& value) { return value.dump(-1, ' ', false); }

UserConfig UserConfig::load() {
  const auto path = config_directory() / "config.json";
  if (!fs::exists(path)) return {};
  try {
    const auto data = json::parse(read_file(path));
    UserConfig config;
    if (data.contains("api_key") && !data["api_key"].is_null())
      config.api_key = data["api_key"].get<std::string>();
    config.host = data.value("host", "127.0.0.1");
    config.port = data.value("port", 8000);
    if (data.contains("codex_command") && !data["codex_command"].is_null())
      config.codex_command = data["codex_command"].get<std::string>();
    if (data.contains("claude_command") && !data["claude_command"].is_null())
      config.claude_command = data["claude_command"].get<std::string>();
    if (config.host != "127.0.0.1" && config.host != "localhost" && config.host != "::1")
      throw std::runtime_error("host must be localhost");
    if (config.port < 1 || config.port > 65535)
      throw std::runtime_error("port must be between 1 and 65535");
    return config;
  } catch (const std::exception& error) {
    throw Error("Invalid Kessel config at " + path.string() + ": " + error.what(), 1,
                "configuration_error");
  }
}

void UserConfig::save() const {
  json data = {{"api_key", api_key ? json(*api_key) : json(nullptr)},
               {"host", host}, {"port", port},
               {"codex_command", codex_command ? json(*codex_command) : json(nullptr)},
               {"claude_command", claude_command ? json(*claude_command) : json(nullptr)}};
  write_file_atomic(config_directory() / "config.json", data.dump(2) + "\n");
}

std::string UserConfig::base_url() const {
  return "http://" + (host == "::1" ? "[::1]" : host) + ":" + std::to_string(port);
}
UserConfig UserConfig::with_generated_key() const {
  if (api_key && !api_key->empty()) return *this;
  auto copy = *this; copy.api_key = random_key(); return copy;
}
UserConfig UserConfig::with_rotated_key() const {
  auto copy = *this; copy.api_key = random_key(); return copy;
}

Settings Settings::load() {
  const auto config = UserConfig::load();
  Settings result;
  auto env_key = environment("KESSEL_API_KEY");
  result.api_key = !env_key.empty() ? std::optional(env_key) : config.api_key;
  result.reload_api_key_from_config = env_key.empty();
  result.listen_host = config.host; result.listen_port = config.port;
  result.request_timeout_seconds = environment_positive_int("KESSEL_REQUEST_TIMEOUT_SECONDS", 300);
  result.max_concurrent_requests = environment_positive_int("KESSEL_MAX_CONCURRENT_REQUESTS", 2);
  result.codex_max_concurrent_requests = environment_positive_int(
      "KESSEL_CODEX_MAX_CONCURRENT_REQUESTS", result.max_concurrent_requests);
  result.claude_max_concurrent_requests = environment_positive_int(
      "KESSEL_CLAUDE_MAX_CONCURRENT_REQUESTS", result.max_concurrent_requests);
  result.provider_slot_wait_seconds = environment_positive_int("KESSEL_PROVIDER_SLOT_WAIT_SECONDS", 5);
  result.shutdown_grace_seconds = environment_positive_int("KESSEL_SHUTDOWN_GRACE_SECONDS", 5);
  result.max_output_bytes = static_cast<std::size_t>(environment_positive_int("KESSEL_MAX_OUTPUT_BYTES", 1'048'576));
  result.max_request_bytes = static_cast<std::size_t>(environment_positive_int("KESSEL_MAX_REQUEST_BYTES", 1'048'576));
  result.codex_command = environment("KESSEL_CODEX_COMMAND",
      config.codex_command.value_or("codex"));
  result.claude_command = environment("KESSEL_CLAUDE_COMMAND",
      config.claude_command.value_or("claude"));
  result.enforce_cli_versions = environment_bool("KESSEL_ENFORCE_CLI_VERSIONS", true);
  auto origins = environment("KESSEL_CORS_ORIGINS",
      "http://127.0.0.1:" + std::to_string(result.listen_port) +
      ",http://localhost:" + std::to_string(result.listen_port) +
      ",http://[::1]:" + std::to_string(result.listen_port));
  for (auto& origin : split(origins, ',')) if (!(origin = trim(origin)).empty()) result.cors_origins.push_back(origin);
  return result;
}

std::optional<fs::path> find_executable(const std::string& command) {
  fs::path direct(command);
  if (direct.has_parent_path() && fs::is_regular_file(direct)) return fs::absolute(direct);
#ifdef _WIN32
  std::wstring wide(command.begin(), command.end());
  std::array<wchar_t, 32768> buffer{};
  const auto length = SearchPathW(nullptr, wide.c_str(), L".exe", buffer.size(), buffer.data(), nullptr);
  if (length > 0 && length < buffer.size()) return fs::path(buffer.data());
  for (const auto& directory : split(environment("PATH"), ';')) {
    for (const auto* extension : {".cmd", ".bat", ".ps1"}) {
      auto shim = fs::path(directory) / (command + extension);
      if (!fs::is_regular_file(shim)) continue;
      if (lower(command) == "codex") {
        const bool arm = lower(environment("PROCESSOR_ARCHITECTURE")).find("arm") != std::string::npos;
        const auto package = arm ? "codex-win32-arm64" : "codex-win32-x64";
        const auto target = arm ? "aarch64-pc-windows-msvc" : "x86_64-pc-windows-msvc";
        auto native = shim.parent_path() / "node_modules" / "@openai" / "codex" /
            "node_modules" / "@openai" / package / "vendor" / target / "bin" / "codex.exe";
        if (fs::is_regular_file(native)) return fs::absolute(native);
      }
      // Batch and PowerShell shims require a shell. Provider execution never
      // invokes one, so only return a safely resolved native binary.
    }
  }
#else
  for (const auto& directory : split(environment("PATH"), ':')) {
    auto candidate = fs::path(directory) / command;
    if (::access(candidate.c_str(), X_OK) == 0) return candidate;
  }
#endif
  return std::nullopt;
}

json Usage::to_openai() const {
  return {{"prompt_tokens", prompt_tokens}, {"completion_tokens", completion_tokens},
          {"total_tokens", prompt_tokens + completion_tokens},
          {"prompt_tokens_details", {{"cached_tokens", cached_tokens}}}};
}
json ToolCall::to_openai() const {
  return {{"id", id}, {"type", "function"},
          {"function", {{"name", name}, {"arguments", arguments}}}};
}

static void validation(bool condition, std::string message,
                       std::optional<std::string> parameter = std::nullopt) {
  if (!condition) throw Error(std::move(message), 400, "validation_error", std::move(parameter));
}

static bool strict_positive_int(const json& value) {
  return value.is_number_integer() && !value.is_boolean() && value.get<long long>() > 0;
}

static std::string message_text(const json& message) {
  if (!message.contains("content") || message["content"].is_null()) {
    if (message.contains("tool_calls")) return json_compact(message["tool_calls"]);
    return {};
  }
  if (message["content"].is_string()) return message["content"].get<std::string>();
  validation(message["content"].is_array(), "Input should be a valid string or list", "messages.content");
  std::string text;
  for (const auto& part : message["content"]) {
    validation(part.is_object() && part.value("type", "") == "text" && part.contains("text") && part["text"].is_string(),
               "Invalid text content part", "messages.content");
    if (!text.empty()) text += '\n';
    text += part["text"].get<std::string>();
  }
  return text;
}

void validate_schema_shape(const json& schema, int depth) {
  if (depth == 0) {
    std::function<void(const json&, int)> scan = [&](const json& node, int level) {
      validation(level <= 32, "JSON schemas may not exceed 32 nested levels");
      if (node.is_object()) {
        validation(!node.contains("$ref"), "JSON Schema references are not supported");
        for (const auto& [_, child] : node.items()) scan(child, level + 1);
      } else if (node.is_array()) for (const auto& child : node) scan(child, level + 1);
    };
    scan(schema, 0);
  }
  validation(schema.is_object() || schema.is_boolean(),
             "invalid JSON schema: schema must be an object or boolean");
  if (schema.is_object()) {
    if (schema.contains("type")) {
      static const std::set<std::string> types = {"null", "boolean", "object", "array", "number", "integer", "string"};
      const auto& declared = schema["type"];
      if (declared.is_string()) validation(types.contains(declared.get<std::string>()), "invalid JSON schema: unknown type");
      else {
        validation(declared.is_array() && !declared.empty(), "invalid JSON schema: type must be a string or non-empty array");
        std::set<std::string> seen;
        for (const auto& type : declared) validation(type.is_string() && types.contains(type.get<std::string>()) && seen.insert(type.get<std::string>()).second, "invalid JSON schema: invalid or duplicate type");
      }
    }
    if (schema.contains("required")) { validation(schema["required"].is_array(), "invalid JSON schema: required must be an array"); std::set<std::string> seen; for (const auto& key : schema["required"]) validation(key.is_string() && seen.insert(key.get<std::string>()).second, "invalid JSON schema: required values must be unique strings"); }
    for (const char* key : {"properties", "patternProperties", "dependentSchemas"}) if (schema.contains(key)) validation(schema[key].is_object(), std::string("invalid JSON schema: ") + key + " must be an object");
    for (const char* key : {"allOf", "anyOf", "oneOf", "prefixItems"}) if (schema.contains(key)) validation(schema[key].is_array() && !schema[key].empty(), std::string("invalid JSON schema: ") + key + " must be a non-empty array");
    for (const char* key : {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}) if (schema.contains(key)) validation(schema[key].is_number() && (std::string(key) != "multipleOf" || schema[key].get<double>() > 0), std::string("invalid JSON schema: ") + key + " must be numeric");
    for (const char* key : {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "minContains", "maxContains"}) if (schema.contains(key)) validation(schema[key].is_number_unsigned() || (schema[key].is_number_integer() && schema[key].get<long long>() >= 0), std::string("invalid JSON schema: ") + key + " must be a non-negative integer");
    if (schema.contains("enum")) validation(schema["enum"].is_array() && !schema["enum"].empty(), "invalid JSON schema: enum must be a non-empty array");
    if (schema.contains("uniqueItems")) validation(schema["uniqueItems"].is_boolean(), "invalid JSON schema: uniqueItems must be a boolean");
    if (schema.contains("pattern")) { validation(schema["pattern"].is_string(), "invalid JSON schema: pattern must be a string"); try { std::regex compiled(schema["pattern"].get<std::string>()); (void)compiled; } catch (...) { validation(false, "invalid JSON schema: invalid pattern"); } }
    for (const char* key : {"additionalProperties", "items", "contains", "not", "if", "then", "else", "propertyNames"}) if (schema.contains(key)) validate_schema_shape(schema[key], depth + 1);
    for (const char* key : {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}) if (schema.contains(key)) for (const auto& [_, child] : schema[key].items()) validate_schema_shape(child, depth + 1);
    for (const char* key : {"allOf", "anyOf", "oneOf", "prefixItems"}) if (schema.contains(key)) for (const auto& child : schema[key]) validate_schema_shape(child, depth + 1);
  }
}

static bool schema_type_matches(const std::string& type, const json& value) {
  if (type == "object") return value.is_object();
  if (type == "array") return value.is_array();
  if (type == "string") return value.is_string();
  if (type == "integer") return value.is_number_integer() || value.is_number_unsigned();
  if (type == "number") return value.is_number();
  if (type == "boolean") return value.is_boolean();
  if (type == "null") return value.is_null();
  return false;
}

static std::size_t utf8_length(std::string_view value) {
  return static_cast<std::size_t>(std::count_if(value.begin(), value.end(),
      [](unsigned char byte) { return (byte & 0xc0) != 0x80; }));
}

bool validate_json_value(const json& schema, const json& value) {
  if (schema.is_boolean()) return schema.get<bool>();
  if (!schema.is_object()) return false;
  if (schema.contains("enum") && std::find(schema["enum"].begin(), schema["enum"].end(), value) == schema["enum"].end()) return false;
  if (schema.contains("const") && schema["const"] != value) return false;
  if (schema.contains("allOf")) for (const auto& child : schema["allOf"]) if (!validate_json_value(child, value)) return false;
  if (schema.contains("anyOf") && std::none_of(schema["anyOf"].begin(), schema["anyOf"].end(), [&](const auto& child) { return validate_json_value(child, value); })) return false;
  if (schema.contains("oneOf") && std::count_if(schema["oneOf"].begin(), schema["oneOf"].end(), [&](const auto& child) { return validate_json_value(child, value); }) != 1) return false;
  if (schema.contains("not") && validate_json_value(schema["not"], value)) return false;
  if (schema.contains("if")) { const bool condition = validate_json_value(schema["if"], value); if (condition && schema.contains("then") && !validate_json_value(schema["then"], value)) return false; if (!condition && schema.contains("else") && !validate_json_value(schema["else"], value)) return false; }
  if (schema.contains("type")) {
    const auto& type = schema["type"];
    const bool matches = type.is_string() ? schema_type_matches(type.get<std::string>(), value) :
        std::any_of(type.begin(), type.end(), [&](const auto& item) { return schema_type_matches(item.template get<std::string>(), value); });
    if (!matches) return false;
  }
  if (value.is_object()) {
    if (!value.is_object()) return false;
    if (schema.contains("required")) for (const auto& key : schema["required"])
      if (!key.is_string() || !value.contains(key.get<std::string>())) return false;
    if (schema.contains("minProperties") && value.size() < schema["minProperties"].get<std::size_t>()) return false;
    if (schema.contains("maxProperties") && value.size() > schema["maxProperties"].get<std::size_t>()) return false;
    if (schema.contains("properties")) {
      for (const auto& [key, child] : schema["properties"].items())
        if (value.contains(key) && !validate_json_value(child, value[key])) return false;
    }
    for (const auto& [key, child] : value.items()) {
      bool covered = schema.contains("properties") && schema["properties"].contains(key);
      if (schema.contains("patternProperties")) for (const auto& [pattern, rule] : schema["patternProperties"].items()) if (std::regex_search(key, std::regex(pattern))) { covered = true; if (!validate_json_value(rule, child)) return false; }
      if (!covered && schema.contains("additionalProperties")) { const auto& rule = schema["additionalProperties"]; if (rule.is_boolean() && !rule.get<bool>()) return false; if (rule.is_object() && !validate_json_value(rule, child)) return false; }
    }
    if (schema.contains("dependentRequired")) for (const auto& [key, dependencies] : schema["dependentRequired"].items()) if (value.contains(key)) for (const auto& dependency : dependencies) if (!value.contains(dependency.get<std::string>())) return false;
    if (schema.contains("dependentSchemas")) for (const auto& [key, child] : schema["dependentSchemas"].items()) if (value.contains(key) && !validate_json_value(child, value)) return false;
  }
  if (value.is_array()) {
    if (schema.contains("minItems") && value.size() < schema["minItems"].get<std::size_t>()) return false;
    if (schema.contains("maxItems") && value.size() > schema["maxItems"].get<std::size_t>()) return false;
    if (schema.value("uniqueItems", false)) for (std::size_t left = 0; left < value.size(); ++left) for (std::size_t right = left + 1; right < value.size(); ++right) if (value[left] == value[right]) return false;
    std::size_t prefix = 0;
    if (schema.contains("prefixItems")) { prefix = schema["prefixItems"].size(); for (std::size_t index = 0; index < std::min(prefix, value.size()); ++index) if (!validate_json_value(schema["prefixItems"][index], value[index])) return false; }
    if (schema.contains("items")) for (std::size_t index = prefix; index < value.size(); ++index) if (!validate_json_value(schema["items"], value[index])) return false;
    if (schema.contains("contains")) { const auto count = std::count_if(value.begin(), value.end(), [&](const auto& item) { return validate_json_value(schema["contains"], item); }); const auto minimum = schema.value("minContains", 1); if (count < minimum || (schema.contains("maxContains") && count > schema["maxContains"].get<int>())) return false; }
  }
  if (value.is_string()) {
    const auto length = utf8_length(value.get_ref<const std::string&>());
    if (schema.contains("minLength") && length < schema["minLength"].get<std::size_t>()) return false;
    if (schema.contains("maxLength") && length > schema["maxLength"].get<std::size_t>()) return false;
    if (schema.contains("pattern") && !std::regex_search(value.get_ref<const std::string&>(), std::regex(schema["pattern"].get<std::string>()))) return false;
  }
  if (value.is_number()) {
    const auto number = value.get<double>();
    if (schema.contains("minimum") && number < schema["minimum"].get<double>()) return false;
    if (schema.contains("maximum") && number > schema["maximum"].get<double>()) return false;
    if (schema.contains("exclusiveMinimum") && number <= schema["exclusiveMinimum"].get<double>()) return false;
    if (schema.contains("exclusiveMaximum") && number >= schema["exclusiveMaximum"].get<double>()) return false;
    if (schema.contains("multipleOf")) { const auto divisor = schema["multipleOf"].get<double>(); const auto remainder = std::remainder(number, divisor); if (std::abs(remainder) > 1e-9 * std::max(1.0, std::abs(number))) return false; }
  }
  return true;
}

ChatRequest parse_chat_request(const json& body) {
  validation(body.is_object(), "Input should be a valid dictionary");
  ChatRequest request;
  validation(body.contains("model") && body["model"].is_string(), "Field required", "model");
  request.model = body["model"].get<std::string>();
  static const std::regex model_pattern(R"(^[A-Za-z0-9][A-Za-z0-9._:/-]*$)");
  validation(!request.model.empty() && request.model.size() <= 200 && std::regex_match(request.model, model_pattern),
             "String should match pattern", "model");
  validation(body.contains("messages") && body["messages"].is_array() && !body["messages"].empty() && body["messages"].size() <= 100,
             "messages must contain 1 to 100 items", "messages");
  request.messages = body["messages"];
  bool content_present = false;
  static const std::set<std::string> roles = {"developer", "system", "user", "assistant", "tool"};
  for (const auto& message : request.messages) {
    validation(message.is_object() && message.contains("role") && message["role"].is_string() && roles.contains(message["role"].get<std::string>()),
               "Invalid message role", "messages.role");
    content_present |= !trim(message_text(message)).empty() || message.contains("tool_call_id") || message.contains("tool_calls");
  }
  validation(content_present, "at least one message must contain text", "messages");
  auto enum_string = [&](const char* key, std::string fallback, const std::set<std::string>& allowed) {
    if (!body.contains(key)) return fallback;
    validation(body[key].is_string() && allowed.contains(body[key].get<std::string>()), "Invalid value", key);
    return body[key].get<std::string>();
  };
  request.reasoning_effort = enum_string("reasoning_effort", "low", {"low", "medium", "high", "xhigh"});
  request.service_tier = enum_string("service_tier", "default", {"default", "fast"});
  request.backend = enum_string("backend", "fresh", {"fresh", "warm"});
  request.tool_choice = enum_string("tool_choice", "auto", {"none", "auto", "required"});
  if (body.contains("stream")) { validation(body["stream"].is_boolean(), "Input should be a valid boolean", "stream"); request.stream = body["stream"]; }
  if (body.contains("parallel_tool_calls")) { validation(body["parallel_tool_calls"].is_boolean(), "Input should be a valid boolean", "parallel_tool_calls"); request.parallel_tool_calls = body["parallel_tool_calls"]; }
  if (request.parallel_tool_calls)
    throw Error("Unsupported parameter 'parallel_tool_calls': parallel function calls are not supported", 400, "unsupported_parameter", "parallel_tool_calls");
  if (body.contains("stream_options") && body["stream_options"].is_object()) request.include_usage = body["stream_options"].value("include_usage", false);
  if (body.contains("n")) { validation(strict_positive_int(body["n"]), "Input should be greater than 0", "n"); request.n = body["n"]; }
  for (const char* key : {"max_tokens", "max_completion_tokens"}) if (body.contains(key) && !body[key].is_null()) {
    validation(strict_positive_int(body[key]), "Input should be a valid positive integer", key);
    if (std::string(key) == "max_tokens") request.max_tokens = body[key]; else request.max_completion_tokens = body[key];
  }
  if (body.contains("stop") && !body["stop"].is_null()) {
    if (body["stop"].is_string()) request.stop.push_back(body["stop"]);
    else { validation(body["stop"].is_array() && body["stop"].size() <= 4, "at most 4 stop sequences are supported", "stop");
      for (const auto& item : body["stop"]) { validation(item.is_string() && !item.get<std::string>().empty(), "stop sequences must not be empty", "stop"); request.stop.push_back(item); } }
    validation(std::all_of(request.stop.begin(), request.stop.end(), [](const auto& item) { return !item.empty(); }), "stop sequences must not be empty", "stop");
  }
  if (body.contains("tools")) {
    validation(body["tools"].is_array() && body["tools"].size() <= 16, "Invalid tools", "tools"); request.tools = body["tools"];
    for (const auto& tool : request.tools) {
      validation(tool.is_object() && tool.value("type", "") == "function" && tool.contains("function") && tool["function"].is_object(), "Invalid function tool", "tools");
      const auto& function = tool["function"];
      const auto name = function.value("name", "");
      validation(std::regex_match(name, std::regex(R"(^[A-Za-z0-9_-]{1,64}$)")), "Invalid function name", "tools");
      validation(!function.value("strict", false), "strict tool schemas are not supported", "tools");
      validate_schema_shape(function.value("parameters", json{{"type", "object"}}));
    }
  }
  const auto active_tools = request.tool_choice == "none" ? 0 : request.tools.size();
  validation(active_tools <= 1, "only one function tool is supported per request", "tools");
  validation(active_tools == 0 || request.tool_choice == "required", "tool_choice must be required when a function tool is supplied", "tool_choice");
  if (body.contains("response_format") && !body["response_format"].is_null()) {
    validation(body["response_format"].is_object(), "Invalid response format", "response_format"); request.response_format = body["response_format"];
    const auto type = request.response_format->value("type", "");
    validation(type == "text" || type == "json_object" || type == "json_schema", "Invalid response format type", "response_format.type");
    if (type == "json_schema") {
      validation(request.response_format->contains("json_schema") && (*request.response_format)["json_schema"].is_object() && (*request.response_format)["json_schema"].contains("schema"), "json_schema is required when type is json_schema", "response_format.json_schema");
      validate_schema_shape((*request.response_format)["json_schema"]["schema"]);
    }
  }
  if (!request.stop.empty() && ((request.response_format && request.response_format->value("type", "text") != "text") || !request.tools.empty()))
    throw Error("Unsupported parameter 'stop': stop sequences are not supported with structured output or tools", 400, "unsupported_parameter", "stop");
  if (request.n != 1) throw Error("Unsupported parameter 'n': only n=1 is supported", 400, "unsupported_parameter", "n");
  return request;
}

ChatRequest parse_anthropic_request(const json& body) {
  validation(body.is_object(), "Input should be a valid dictionary");
  json converted;
  converted["model"] = body.value("model", "default");
  converted["stream"] = body.value("stream", false);
  converted["backend"] = body.value("backend", "fresh");
  converted["reasoning_effort"] = body.value("reasoning_effort", "low");
  converted["stream_options"] = {{"include_usage", true}};
  if (body.contains("max_tokens")) converted["max_tokens"] = body["max_tokens"];
  if (body.contains("stop_sequences")) converted["stop"] = body["stop_sequences"];
  json messages = json::array();
  if (body.contains("system")) {
    std::string system;
    if (body["system"].is_string()) system = body["system"];
    else if (body["system"].is_array()) for (const auto& block : body["system"])
      if (block.value("type", "") == "text" && block.contains("text")) { if (!system.empty()) system += '\n'; system += block["text"].get<std::string>(); }
    if (!system.empty()) messages.push_back({{"role", "system"}, {"content", system}});
  }
  validation(body.contains("messages") && body["messages"].is_array(), "Field required", "messages");
  for (const auto& message : body["messages"]) {
    std::string text;
    if (message.contains("content") && message["content"].is_string()) text = message["content"];
    else if (message.contains("content") && message["content"].is_array()) for (const auto& block : message["content"]) {
      if (block.value("type", "") == "text") text += (text.empty() ? "" : "\n") + block.value("text", "");
      else if (block.value("type", "") == "tool_result") text += "Tool result for " + block.value("tool_use_id", "unknown") + ": " + (block.contains("content") && block["content"].is_string() ? block["content"].get<std::string>() : json_compact(block.value("content", json(""))));
      else if (block.value("type", "") == "tool_use") text += "Previous tool call: " + json_compact(block);
    }
    messages.push_back({{"role", message.value("role", "")}, {"content", text}});
  }
  converted["messages"] = messages;
  if (body.contains("tools")) {
    validation(body["tools"].is_array() && body["tools"].size() <= 1, "List should have at most 1 item", "tools");
    converted["tools"] = json::array();
    for (const auto& tool : body["tools"]) converted["tools"].push_back({{"type", "function"}, {"function", {{"name", tool.value("name", "")}, {"description", tool.value("description", "")}, {"parameters", tool.value("input_schema", json{{"type", "object"}})}}}});
    auto choice = body.contains("tool_choice") && body["tool_choice"].is_object() ? body["tool_choice"].value("type", "auto") : "auto";
    if (choice == "tool") {
      const auto selected = body["tool_choice"].value("name", "");
      validation(!body["tools"].empty() && selected == body["tools"][0].value("name", ""), "tool_choice name must match the supplied tool", "tool_choice.name");
    }
    converted["tool_choice"] = (choice == "any" || choice == "tool") ? "required" : choice;
  }
  if (body.contains("stop_sequences") && !body.value("tools", json::array()).empty())
    throw Error("Unsupported parameter 'stop_sequences': stop sequences are not supported with tools", 400, "unsupported_parameter", "stop_sequences");
  return parse_chat_request(converted);
}

std::string build_prompt(const ChatRequest& request) {
  std::string result = "Answer the following chat conversation. Treat DEVELOPER and SYSTEM messages as instructions. Return only the assistant response. Do not inspect the local machine, run commands, or use tools.";
  static const std::map<std::string, std::string> labels = {{"developer", "DEVELOPER"}, {"system", "SYSTEM"}, {"user", "USER"}, {"assistant", "ASSISTANT"}, {"tool", "TOOL"}};
  for (const auto& message : request.messages) {
    auto text = trim(message_text(message)); if (text.empty()) continue;
    auto label = labels.at(message["role"].get<std::string>());
    if (message.contains("tool_call_id") && message["tool_call_id"].is_string()) label += " call_id=" + message["tool_call_id"].get<std::string>();
    result += "\n\n[" + label + "]\n" + text;
  }
  if (!request.tools.empty() && request.tool_choice != "none") {
    result += "\n\n[AVAILABLE_FUNCTIONS]\n" + json_compact(request.tools);
    result += "\n\n[TOOL_INSTRUCTIONS]\nYou must call the supplied function. Produce at most one function call.";
  }
  if (auto schema = output_schema(request)) result += "\n\n[OUTPUT_SCHEMA]\n" + json_compact(*schema) + "\nReturn only one JSON value that conforms to this schema.";
  return result + "\n\n[ASSISTANT]";
}

std::optional<json> output_schema(const ChatRequest& request) {
  if (!request.tools.empty() && request.tool_choice != "none") {
    const auto& function = request.tools[0]["function"];
    return json{{"type", "object"}, {"properties", {{"name", {{"type", "string"}, {"enum", json::array({function["name"]})}}}, {"arguments", function.value("parameters", json{{"type", "object"}})}}}, {"required", json::array({"name", "arguments"})}, {"additionalProperties", false}};
  }
  if (!request.response_format || request.response_format->value("type", "text") == "text") return std::nullopt;
  if (request.response_format->value("type", "") == "json_object") return json{{"type", "object"}};
  return (*request.response_format)["json_schema"]["schema"];
}

ProviderResult parse_structured_result(const ChatRequest& request, ProviderResult result) {
  const auto schema = output_schema(request); if (!schema) return result;
  if (!result.text) throw ProcessError("provider returned no structured output");
  json payload;
  try { payload = json::parse(*result.text); } catch (...) { throw ProcessError("provider returned invalid structured JSON"); }
  if (!validate_json_value(*schema, payload)) throw ProcessError("provider returned structured output that does not match the schema");
  if (request.tools.empty() || request.tool_choice == "none") { result.text = json_compact(payload); return result; }
  const auto name = payload.value("name", "");
  if (name != request.tools[0]["function"]["name"].get<std::string>() || !payload.value("arguments", json()).is_object()) throw ProcessError("provider returned an unknown or invalid function call");
  result.text.reset(); result.tool_calls.push_back({request_id("call_local_"), name, json_compact(payload["arguments"])}); return result;
}

static std::shared_ptr<GptEncoding> tokenizer() {
  static auto value = GptEncoding::get_encoding(LanguageModel::O200K_BASE);
  return value;
}
int estimate_tokens(std::string_view text) {
  return static_cast<int>(tokenizer()->encode(std::string(text)).size());
}
std::string token_prefix(std::string_view text, int limit) {
  if (limit <= 0) return {};
  auto tokens = tokenizer()->encode(std::string(text));
  if (static_cast<int>(tokens.size()) <= limit) return std::string(text);
  tokens.resize(static_cast<std::size_t>(limit));
  auto prefix = tokenizer()->decode(tokens);
  while (!prefix.empty() && !text.starts_with(prefix)) prefix.pop_back();
  return prefix;
}

ProviderResult apply_output_controls(const ChatRequest& request, ProviderResult result) {
  if (!result.text && !result.tool_calls.empty()) {
    const int limit = request.max_tokens && request.max_completion_tokens ? std::min(*request.max_tokens, *request.max_completion_tokens) : request.max_tokens.value_or(request.max_completion_tokens.value_or(0));
    if (limit > 0) { std::string tool_text; for (const auto& tool : result.tool_calls) tool_text += tool.name + tool.arguments;
      const int tokens = estimate_tokens(tool_text); if (tokens >= limit) { result.finish_reason = "length"; if (tokens > limit) result.tool_calls.clear(); result.usage = Usage{result.usage ? result.usage->prompt_tokens : 0, tokens > limit ? 0 : tokens, result.usage ? result.usage->cached_tokens : 0}; } }
    return result;
  }
  if (!result.text) return result;
  std::size_t stop_index = std::string::npos; std::optional<std::string> stop;
  for (const auto& sequence : request.stop) { const auto index = result.text->find(sequence); if (index < stop_index) { stop_index = index; stop = sequence; } }
  if (stop) { result.text = result.text->substr(0, stop_index); result.finish_reason = "stop"; result.stop_sequence = stop; }
  const int limit = request.max_tokens && request.max_completion_tokens ? std::min(*request.max_tokens, *request.max_completion_tokens) : request.max_tokens.value_or(request.max_completion_tokens.value_or(0));
  if (limit > 0 && estimate_tokens(*result.text) >= limit) { result.text = token_prefix(*result.text, limit); result.finish_reason = "length"; result.stop_sequence.reset(); }
  if (result.finish_reason) result.usage = Usage{result.usage ? result.usage->prompt_tokens : 0, estimate_tokens(*result.text), result.usage ? result.usage->cached_tokens : 0};
  return result;
}

SlotPool::Lease::~Lease() { if (pool_) pool_->release(); }
SlotPool::Lease SlotPool::acquire(int timeout_seconds, std::string_view provider) {
  std::unique_lock lock(mutex_);
  if (!condition_.wait_for(lock, std::chrono::seconds(timeout_seconds), [&] { return available_ > 0; })) {
    ProcessError error(std::string(provider) + " concurrency limit reached", ProcessError::Kind::busy); error.retry_after = std::max(1, timeout_seconds); throw error;
  }
  --available_; return Lease(this);
}
void SlotPool::release() { { std::lock_guard lock(mutex_); ++available_; } condition_.notify_one(); }

bool Provider::accepts_model(const std::string& model) {
  if (lower(model) == "default" || lower(model) == name()) return true;
  std::lock_guard lock(observed_mutex_); return std::find(observed_models_.begin(), observed_models_.end(), model) != observed_models_.end();
}
void Provider::observe_model(const std::string& model) {
  if (model.empty() || lower(model) == "default" || lower(model) == name()) return;
  std::lock_guard lock(observed_mutex_); if (std::find(observed_models_.begin(), observed_models_.end(), model) == observed_models_.end()) observed_models_.push_back(model);
}

Registry::Registry(const Settings& settings)
    : codex(make_codex_provider(settings)), claude(make_claude_provider(settings)),
      codex_slots(settings.codex_max_concurrent_requests),
      claude_slots(settings.claude_max_concurrent_requests),
      slot_wait_seconds(settings.provider_slot_wait_seconds) {}
Provider& Registry::get(const std::string& name) { if (name == "codex") return *codex; if (name == "claude") return *claude; throw Error("Unknown provider", 404, "not_found"); }
SlotPool& Registry::slots(const std::string& name) { return name == "codex" ? codex_slots : claude_slots; }
void Registry::close() { codex->close(); claude->close(); }

}  // namespace kessel
