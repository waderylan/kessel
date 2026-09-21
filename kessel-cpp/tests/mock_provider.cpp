#include <algorithm>
#include <cctype>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

int main(int argc, char** argv) {
  std::vector<std::string> arguments(argv + 1, argv + argc);
  if (std::find(arguments.begin(), arguments.end(), "--version") != arguments.end()) {
    std::string executable = argv[0];
    std::transform(executable.begin(), executable.end(), executable.begin(),
                   [](unsigned char value) { return std::tolower(value); });
    std::cout << (executable.find("claude") == std::string::npos
                      ? "codex-cli 0.155.1\n"
                      : "2.1.278 (Claude Code)\n");
    return 0;
  }
  if (std::find(arguments.begin(), arguments.end(), "status") != arguments.end()) {
    std::cout << "Logged in\n";
    return 0;
  }
  const std::string prompt{std::istreambuf_iterator<char>(std::cin), {}};
  const bool codex = std::find(arguments.begin(), arguments.end(), "exec") != arguments.end();
  const bool streaming = std::find(arguments.begin(), arguments.end(), "stream-json") != arguments.end();
  if (codex) {
    std::cout << json{{"type", "item.completed"}, {"item", {{"type", "agent_message"}, {"text", "MOCK_OK"}}}} << '\n';
    std::cout << json{{"type", "turn.completed"}, {"usage", {{"input_tokens", 4}, {"cached_input_tokens", 0}, {"output_tokens", 2}}}} << '\n';
  } else if (streaming) {
    std::cout << json{{"type", "stream_event"}, {"event", {{"type", "content_block_delta"}, {"delta", {{"type", "text_delta"}, {"text", "MOCK_OK"}}}}}} << '\n';
    std::cout << json{{"type", "result"}, {"is_error", false}, {"result", "MOCK_OK"}, {"usage", {{"input_tokens", 4}, {"output_tokens", 2}}}} << '\n';
  } else {
    std::cout << json{{"is_error", false}, {"result", "MOCK_OK"}, {"usage", {{"input_tokens", 4}, {"output_tokens", 2}}}};
  }
  return 0;
}
