#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

namespace kessel {

using json = nlohmann::json;
namespace fs = std::filesystem;

struct Error : std::runtime_error {
  int status;
  std::string code;
  std::optional<std::string> parameter;
  explicit Error(std::string message, int status_ = 502,
                 std::string code_ = "provider_error",
                 std::optional<std::string> parameter_ = std::nullopt)
      : std::runtime_error(std::move(message)), status(status_),
        code(std::move(code_)), parameter(std::move(parameter_)) {}
};

struct ProcessError : Error {
  enum class Kind { generic, not_found, timeout, output_limit, exit, rate_limit,
                    busy, authentication };
  Kind kind;
  int retry_after = 0;
  std::string provider;
  std::string internal_detail;
  explicit ProcessError(std::string message, Kind kind_ = Kind::generic)
      : Error(std::move(message)), kind(kind_) {}
};

struct Settings {
  std::optional<std::string> api_key;
  std::vector<std::string> cors_origins;
  int request_timeout_seconds = 300;
  int max_concurrent_requests = 2;
  int codex_max_concurrent_requests = 2;
  int claude_max_concurrent_requests = 2;
  int provider_slot_wait_seconds = 5;
  int shutdown_grace_seconds = 5;
  std::size_t max_output_bytes = 1'048'576;
  std::size_t max_request_bytes = 1'048'576;
  std::string codex_command = "codex";
  std::string claude_command = "claude";
  bool enforce_cli_versions = true;
  std::string expected_codex_version = "0.155.1";
  std::string expected_claude_version = "2.1.278";
  std::string listen_host = "127.0.0.1";
  int listen_port = 8000;
  bool reload_api_key_from_config = false;
  bool allow_unauthenticated = false;
  static Settings load();
};

struct UserConfig {
  std::optional<std::string> api_key;
  std::string host = "127.0.0.1";
  int port = 8000;
  std::optional<std::string> codex_command;
  std::optional<std::string> claude_command;
  static UserConfig load();
  void save() const;
  std::string base_url() const;
  UserConfig with_generated_key() const;
  UserConfig with_rotated_key() const;
};

fs::path config_directory();
fs::path state_directory();
fs::path asset_root();
std::string environment(std::string_view name, std::string fallback = {});
bool environment_bool(std::string_view name, bool fallback);
int environment_positive_int(std::string_view name, int fallback);
std::string random_hex(std::size_t bytes);
std::string random_key();
std::string request_id(std::string_view prefix);
bool constant_time_equal(std::string_view left, std::string_view right);
std::string trim(std::string value);
std::string lower(std::string value);
std::vector<std::string> split(std::string_view input, char delimiter);
std::string read_file(const fs::path& path);
void write_file_atomic(const fs::path& path, std::string_view value);
std::optional<fs::path> find_executable(const std::string& command);
std::string json_compact(const json& value);

struct Usage {
  int prompt_tokens = 0;
  int completion_tokens = 0;
  int cached_tokens = 0;
  json to_openai() const;
};

struct ToolCall {
  std::string id;
  std::string name;
  std::string arguments;
  json to_openai() const;
};

struct ProviderResult {
  std::optional<std::string> text;
  std::string model;
  std::optional<Usage> usage;
  std::vector<ToolCall> tool_calls;
  std::optional<std::string> finish_reason;
  std::optional<std::string> stop_sequence;
};

struct ChatRequest {
  std::string model;
  json messages;
  std::string reasoning_effort = "low";
  std::string service_tier = "default";
  std::string backend = "fresh";
  bool stream = false;
  bool include_usage = false;
  json tools = json::array();
  std::string tool_choice = "auto";
  bool parallel_tool_calls = false;
  std::optional<json> response_format;
  std::vector<std::string> stop;
  std::optional<int> max_tokens;
  std::optional<int> max_completion_tokens;
  int n = 1;
};

ChatRequest parse_chat_request(const json& body);
ChatRequest parse_anthropic_request(const json& body);
std::string build_prompt(const ChatRequest& request);
std::optional<json> output_schema(const ChatRequest& request);
void validate_schema_shape(const json& schema, int depth = 0);
bool validate_json_value(const json& schema, const json& value);
ProviderResult parse_structured_result(const ChatRequest& request,
                                       ProviderResult result);
int estimate_tokens(std::string_view text);
std::string token_prefix(std::string_view text, int limit);
ProviderResult apply_output_controls(const ChatRequest& request,
                                     ProviderResult result);

struct ProcessResult {
  std::string out;
  std::string err;
  int exit_code = 0;
};

using LineCallback = std::function<bool(std::string_view)>;
ProcessResult run_process(const std::vector<std::string>& command,
                          std::string_view input, const fs::path& cwd,
                          int timeout_seconds, std::size_t max_output_bytes,
                          const std::map<std::string, std::string>& overrides = {},
                          const LineCallback& stdout_line = {});
std::map<std::string, std::string>
child_environment(const std::map<std::string, std::string>& overrides = {});

class SlotPool {
 public:
  explicit SlotPool(int capacity) : capacity_(capacity), available_(capacity) {}
  class Lease {
   public:
    Lease() = default;
    explicit Lease(SlotPool* pool) : pool_(pool) {}
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;
    Lease(Lease&& other) noexcept : pool_(other.pool_) { other.pool_ = nullptr; }
    ~Lease();
   private:
    SlotPool* pool_ = nullptr;
  };
  Lease acquire(int timeout_seconds, std::string_view provider);
 private:
  void release();
  int capacity_;
  int available_;
  std::mutex mutex_;
  std::condition_variable condition_;
  friend class Lease;
};

class Provider {
 public:
  virtual ~Provider() = default;
  virtual std::string name() const = 0;
  virtual std::string command() const = 0;
  virtual ProviderResult complete(const ChatRequest& request,
                                  const LineCallback& delta = {}) = 0;
  virtual std::vector<std::string> models() = 0;
  virtual bool accepts_model(const std::string& model);
  virtual void close() {}
 protected:
  std::mutex observed_mutex_;
  std::vector<std::string> observed_models_;
  void observe_model(const std::string& model);
};

std::unique_ptr<Provider> make_codex_provider(const Settings& settings);
std::unique_ptr<Provider> make_claude_provider(const Settings& settings);

struct Registry {
  std::unique_ptr<Provider> codex;
  std::unique_ptr<Provider> claude;
  SlotPool codex_slots;
  SlotPool claude_slots;
  int slot_wait_seconds;
  explicit Registry(const Settings& settings);
  Provider& get(const std::string& name);
  SlotPool& slots(const std::string& name);
  void close();
};

int run_server(const Settings& settings);
int run_cli(int argc, char** argv);
int self_test();

}  // namespace kessel
