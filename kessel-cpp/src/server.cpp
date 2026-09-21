#include "kessel/core.hpp"

#include <httplib.h>

#include <ctime>
#include <iostream>
#include <regex>

namespace kessel {

static json openai_error(std::string message, std::string type, std::string code,
                         std::optional<std::string> parameter = std::nullopt) {
  return {{"error", {{"message", std::move(message)}, {"type", std::move(type)},
                     {"param", parameter ? json(*parameter) : json(nullptr)},
                     {"code", std::move(code)}}}};
}

static json anthropic_error(std::string message, std::string type,
                            const std::string& id) {
  return {{"type", "error"}, {"error", {{"type", std::move(type)},
          {"message", std::move(message)}}}, {"request_id", id}};
}

static bool anthropic_path(const httplib::Request& request) {
  return request.path == "/v1/messages";
}

static void json_response(httplib::Response& response, int status, const json& body) {
  response.status = status; response.set_content(json_compact(body), "application/json");
}

static void error_response(const httplib::Request& request, httplib::Response& response,
                           const Error& error, const std::string& id) {
  int status = error.status; std::string code = error.code; std::string message = error.what();
  if (const auto* process = dynamic_cast<const ProcessError*>(&error)) {
    switch (process->kind) {
      case ProcessError::Kind::busy: status = 429; code = "provider_busy"; message = "Provider concurrency limit reached"; break;
      case ProcessError::Kind::rate_limit: status = 429; code = "rate_limit_exceeded"; message = "Provider subscription quota is exhausted"; break;
      case ProcessError::Kind::not_found: status = 503; code = "provider_not_installed"; message = "Provider command is not installed"; break;
      case ProcessError::Kind::timeout: status = 504; code = "provider_timeout"; message = "Provider request timed out"; break;
      case ProcessError::Kind::output_limit: status = 502; code = "provider_output_limit"; message = "Provider output exceeded the configured limit"; break;
      case ProcessError::Kind::exit: status = 502; code = "provider_process_failed"; message = "Provider process failed"; break;
      case ProcessError::Kind::authentication: status = 503; code = "provider_not_authenticated"; message = (process->provider == "claude" ? "Claude Code isn't logged in. Run: claude login" : "Codex isn't logged in. Run: codex login"); break;
      default: status = 502; code = "provider_error"; message = "Provider request failed"; break;
    }
    if (process->retry_after > 0) response.set_header("Retry-After", std::to_string(process->retry_after));
    if (process->kind == ProcessError::Kind::rate_limit) {
      response.set_header("Ratelimit-Limit", "100"); response.set_header("Ratelimit-Remaining", "0"); response.set_header("X-Kessel-Quota-Remaining-Percent", "0");
    }
  }
  if (anthropic_path(request)) {
    std::string type = status == 400 ? "invalid_request_error" : status == 401 ? "authentication_error" : status == 403 ? "permission_error" : status == 404 ? "not_found_error" : status == 413 ? "request_too_large" : status == 429 ? "rate_limit_error" : status == 529 ? "overloaded_error" : "api_error";
    json_response(response, status, anthropic_error(message, type, id));
  } else {
    std::string type = status == 401 ? "authentication_error" : status == 403 ? "permission_error" : status == 429 ? "rate_limit_error" : (status >= 400 && status < 500 ? "invalid_request_error" : "server_error");
    json_response(response, status, openai_error(message, type, code, error.parameter));
  }
}

static bool contains(const std::vector<std::string>& values, const std::string& value) {
  return std::find(values.begin(), values.end(), value) != values.end();
}

static std::string auth_key(const httplib::Request& request) {
  auto authorization = request.get_header_value("Authorization");
  if (!authorization.empty()) { auto space = authorization.find(' '); if (space != std::string::npos && lower(authorization.substr(0, space)) == "bearer") return authorization.substr(space + 1); }
  return request.get_header_value("X-API-Key");
}

static void require_key(const httplib::Request& request, const Settings& settings) {
  auto expected = settings.api_key;
  if (settings.reload_api_key_from_config) {
    static std::mutex cache_mutex;
    static fs::path cached_path;
    static fs::file_time_type cached_write_time{};
    static std::optional<std::string> cached_key;
    const auto path = config_directory() / "config.json";
    std::error_code error; const auto write_time = fs::last_write_time(path, error);
    std::lock_guard lock(cache_mutex);
    if (path != cached_path || error || write_time != cached_write_time) {
      cached_key = UserConfig::load().api_key;
      cached_path = path;
      cached_write_time = write_time;
    }
    expected = cached_key;
  }
  if (!expected || expected->empty()) { if (settings.allow_unauthenticated) return; throw Error("Kessel has no API key. Run: kessel setup", 503, "http_error"); }
  if (!constant_time_equal(auth_key(request), *expected)) throw Error("Wrong or missing API key. Send it in Authorization: Bearer <key> or X-API-Key. Run kessel key to see yours", 401, "invalid_api_key");
}

static std::string sse_data(const json& value) { return "data: " + json_compact(value) + "\n\n"; }

struct StreamController {
  explicit StreamController(ChatRequest request, std::function<bool(std::string_view)> emit)
      : request(std::move(request)), emit(std::move(emit)) {
    for (const auto& item : this->request.stop) holdback = std::max(holdback, item.size() - 1);
    limit = this->request.max_tokens && this->request.max_completion_tokens ? std::min(*this->request.max_tokens, *this->request.max_completion_tokens) : this->request.max_tokens.value_or(this->request.max_completion_tokens.value_or(0));
  }
  bool push(std::string_view value) {
    if (terminated) return false;
    pending += value;
    std::size_t earliest = std::string::npos; std::optional<std::string> matched;
    for (const auto& stop : request.stop) { auto position = pending.find(stop); if (position < earliest) { earliest = position; matched = stop; } }
    std::string candidate;
    if (matched) { candidate = pending.substr(0, earliest); pending.clear(); finish = "stop"; stop_sequence = matched; }
    else if (holdback && pending.size() > holdback) { candidate = pending.substr(0, pending.size() - holdback); pending.erase(0, pending.size() - holdback); }
    else if (!holdback) { candidate.swap(pending); }
    if (!candidate.empty() && !emit_limited(candidate)) return false;
    if (matched) { terminated = true; return false; }
    return !terminated;
  }
  void flush() { if (!terminated && !pending.empty()) { auto value = std::move(pending); pending.clear(); emit_limited(value); } }
  bool emit_limited(const std::string& value) {
    if (limit <= 0) { delivered += value; return emit(value); }
    auto combined = delivered + value; if (estimate_tokens(combined) < limit) { delivered = combined; return emit(value); }
    auto limited = token_prefix(combined, limit); auto piece = limited.size() >= delivered.size() ? limited.substr(delivered.size()) : std::string(); delivered = std::move(limited); if (!piece.empty() && !emit(piece)) return false; finish = "length"; terminated = true; return false;
  }
  ChatRequest request; std::function<bool(std::string_view)> emit; std::string pending, delivered; std::size_t holdback = 0; int limit = 0; bool terminated = false; std::optional<std::string> finish, stop_sequence;
};

static json completion_payload(const ProviderResult& result) {
  json tools = nullptr; if (!result.tool_calls.empty()) { tools = json::array(); for (const auto& tool : result.tool_calls) tools.push_back(tool.to_openai()); }
  const auto finish = result.finish_reason.value_or(result.tool_calls.empty() ? "stop" : "tool_calls");
  return {{"id", request_id("chatcmpl-local-")}, {"object", "chat.completion"},
          {"created", std::time(nullptr)}, {"model", result.model},
          {"choices", json::array({{{"index", 0}, {"message", {{"role", "assistant"}, {"content", result.text ? json(*result.text) : json(nullptr)}, {"refusal", nullptr}, {"tool_calls", tools}}}, {"logprobs", nullptr}, {"finish_reason", finish}}})},
          {"usage", result.usage ? result.usage->to_openai() : json(nullptr)}};
}

static ProviderResult execute(Registry& registry, const std::string& provider,
                              const ChatRequest& request, const LineCallback& delta = {}) {
  auto lease = registry.slots(provider).acquire(registry.slot_wait_seconds, provider);
  auto result = registry.get(provider).complete(request, delta);
  return apply_output_controls(request, std::move(result));
}

static std::string provider_from_path(const httplib::Request& request) {
  if (request.matches.size() > 1) return request.matches[1].str();
  const std::regex pattern(R"(^/v1/([^/]+)/(?:models|chat/completions)$)"); std::smatch match;
  if (std::regex_match(request.path, match, pattern)) return match[1];
  return {};
}

static void add_common_headers(const Settings& settings, const httplib::Request& request,
                               httplib::Response& response, const std::string& id) {
  response.set_header("X-Request-ID", id); if (anthropic_path(request)) response.set_header("Request-ID", id);
  auto origin = request.get_header_value("Origin"); if (!origin.empty() && contains(settings.cors_origins, origin)) {
    response.set_header("Access-Control-Allow-Origin", origin); response.set_header("Vary", "Origin");
    response.set_header("Access-Control-Expose-Headers", "request-id,x-request-id,retry-after,ratelimit-limit,ratelimit-remaining,ratelimit-reset,x-kessel-quota-remaining-percent,x-kessel-quota-reset-at,x-kessel-model-discovery");
  }
}

static std::string parse_version(const std::string& text) {
  std::smatch match; static const std::regex pattern(R"(\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b)");
  if (!std::regex_search(text, match, pattern))
    throw Error("could not parse CLI version", 1, "version_error");
  return match[1];
}

static void verify_versions(const Settings& settings) {
  for (const auto& [name, command, expected] : std::vector<std::tuple<std::string, std::string, std::string>>{{"codex", settings.codex_command, settings.expected_codex_version}, {"claude", settings.claude_command, settings.expected_claude_version}}) {
    auto result = run_process({command, "--version"}, "", fs::current_path(), 10, 65'536); auto actual = parse_version(result.out + result.err);
    if (actual != expected) throw Error("Refusing to start with untested CLI versions: " + name + " " + actual + " (tested: " + expected + ")", 1, "version_error");
  }
}

int run_server(const Settings& settings) {
  if (!settings.api_key && !settings.allow_unauthenticated) throw Error("Kessel is not set up. Run: kessel setup", 1, "configuration_error");
  if (settings.enforce_cli_versions) verify_versions(settings);
  Registry registry(settings); httplib::Server server;
  std::mutex health_cache_mutex;
  auto health_cache_time = std::chrono::steady_clock::time_point{};
  json health_cache;
  auto provider_health = [&] {
    std::lock_guard lock(health_cache_mutex);
    const auto now = std::chrono::steady_clock::now();
    if (health_cache.is_null() || now - health_cache_time > std::chrono::seconds(5)) {
      health_cache = {{"codex", {{"available", find_executable(settings.codex_command).has_value()}}},
                      {"claude", {{"available", find_executable(settings.claude_command).has_value()}}}};
      health_cache_time = now;
    }
    return health_cache;
  };
  server.new_task_queue = [] { return new httplib::ThreadPool(8); };
  server.set_payload_max_length(settings.max_request_bytes);
  server.set_pre_routing_handler([&](const httplib::Request& request, httplib::Response& response) {
    const auto id = request_id("req_local_"); response.set_header("X-Request-ID", id); if (anthropic_path(request)) response.set_header("Request-ID", id);
    const auto host = lower(request.get_header_value("Host")); const auto port = std::to_string(settings.listen_port);
    if (host != "127.0.0.1:" + port && host != "localhost:" + port && host != "[::1]:" + port) { error_response(request, response, Error("Invalid Host header", 421, "invalid_host"), id); return httplib::Server::HandlerResponse::Handled; }
    const auto origin = request.get_header_value("Origin"); if (!origin.empty() && !contains(settings.cors_origins, origin)) { error_response(request, response, Error("Origin is not allowed", 403, "origin_not_allowed"), id); return httplib::Server::HandlerResponse::Handled; }
    if (request.method == "POST" && request.path.rfind("/v1/", 0) == 0) { auto type = lower(request.get_header_value("Content-Type")); if (type.substr(0, type.find(';')) != "application/json") { error_response(request, response, Error("Content-Type must be application/json", 415, "unsupported_media_type"), id); return httplib::Server::HandlerResponse::Handled; } }
    return httplib::Server::HandlerResponse::Unhandled;
  });
  server.set_post_routing_handler([&](const httplib::Request& request, httplib::Response& response) {
    auto id = response.get_header_value("X-Request-ID"); if (id.empty()) id = request_id("req_local_"); add_common_headers(settings, request, response, id);
    if (request.path == "/" || request.path.rfind("/static/", 0) == 0) response.set_header("Cache-Control", "no-cache");
  });
  server.set_error_handler([&](const httplib::Request& request, httplib::Response& response) {
    if (lower(response.get_header_value("Content-Type")).rfind("application/json", 0) == 0)
      return;
    auto id = response.get_header_value("X-Request-ID");
    if (id.empty()) { id = request_id("req_local_"); response.set_header("X-Request-ID", id); }
    if (response.status == 413)
      error_response(request, response,
                     Error("Request body is too large", 413, "request_too_large"), id);
    else if (request.path.rfind("/v1/", 0) == 0)
      error_response(request, response, Error("Not Found", 404, "not_found"), id);
  });
  server.Options(R"(/.*)", [&](const httplib::Request& request, httplib::Response& response) {
    auto origin = request.get_header_value("Origin"); if (!origin.empty() && contains(settings.cors_origins, origin)) response.set_header("Access-Control-Allow-Origin", origin);
    response.set_header("Access-Control-Allow-Methods", "GET, POST"); response.set_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key, Anthropic-Version, X-Request-ID"); response.status = 204;
  });
  server.Get("/health", [&](const httplib::Request&, httplib::Response& response) {
    json_response(response, 200, {{"status", "ok"}, {"providers", provider_health()}});
  });
  server.Get(R"(/v1/(codex|claude)/models)", [&](const httplib::Request& request, httplib::Response& response) {
    const auto id = response.get_header_value("X-Request-ID"); try { require_key(request, settings); auto provider = provider_from_path(request); auto lease = registry.slots(provider).acquire(registry.slot_wait_seconds, provider); auto models = registry.get(provider).models(); json data = json::array(); for (const auto& model : models) data.push_back({{"id", model}, {"object", "model"}, {"created", 0}, {"owned_by", provider}}); response.set_header("X-Kessel-Model-Discovery", provider == "codex" ? "provider" : "confirmed-this-process"); json_response(response, 200, {{"object", "list"}, {"data", data}}); } catch (const Error& error) { error_response(request, response, error, id); } catch (const std::exception&) { error_response(request, response, ProcessError("provider request failed"), id); }
  });
  server.Post(R"(/v1/(codex|claude)/chat/completions)", [&](const httplib::Request& request, httplib::Response& response) {
    const auto id = response.get_header_value("X-Request-ID"); try {
      require_key(request, settings); auto provider = provider_from_path(request); auto body = parse_chat_request(json::parse(request.body));
      if (body.backend == "warm" && provider != "codex") throw Error("Warm mode is only available for Codex. Claude stream-json keeps conversation state and cannot provide stateless requests.", 400, "invalid_request");
      if (!registry.get(provider).accepts_model(body.model)) throw Error("Unknown or unapproved model", 400, "invalid_model", "model");
      if (!body.stream) { json_response(response, 200, completion_payload(execute(registry, provider, body))); return; }
      response.set_header("Cache-Control", "no-cache"); response.set_header("X-Accel-Buffering", "no"); response.set_chunked_content_provider("text/event-stream", [&, provider, body](std::size_t, httplib::DataSink& sink) mutable {
        const auto completion_id = request_id("chatcmpl-local-"); const auto created = std::time(nullptr); bool wrote_content = false;
        auto chunk = [&](json delta, json finish = nullptr, json usage = json()) { json value = {{"id", completion_id}, {"object", "chat.completion.chunk"}, {"created", created}, {"model", body.model}, {"choices", json::array({{{"index", 0}, {"delta", std::move(delta)}, {"logprobs", nullptr}, {"finish_reason", finish}}})}}; if (body.include_usage) value["usage"] = usage.is_discarded() ? json(nullptr) : usage; return value; };
        auto initial = sse_data(chunk({{"role", "assistant"}, {"content", ""}}));
        sink.write(initial.data(), initial.size());
        StreamController control(body, [&](std::string_view text) { wrote_content = true; auto encoded = sse_data(chunk({{"content", text}})); return sink.write(encoded.data(), encoded.size()); });
        try { auto result = execute(registry, provider, body, [&](std::string_view text) { return control.push(text); }); control.flush(); if (control.finish) { result.text = control.delivered; result.finish_reason = control.finish; result.stop_sequence = control.stop_sequence; result.usage = Usage{result.usage ? result.usage->prompt_tokens : 0, estimate_tokens(control.delivered), result.usage ? result.usage->cached_tokens : 0}; }
          if (!result.tool_calls.empty()) { auto tool = result.tool_calls[0].to_openai(); tool["index"] = 0; auto encoded = sse_data(chunk({{"tool_calls", json::array({tool})}})); sink.write(encoded.data(), encoded.size()); }
          else if (!wrote_content && result.text) { auto encoded = sse_data(chunk({{"content", *result.text}})); sink.write(encoded.data(), encoded.size()); }
          auto finish = result.finish_reason.value_or(result.tool_calls.empty() ? "stop" : "tool_calls"); auto encoded = sse_data(chunk(json::object(), finish)); sink.write(encoded.data(), encoded.size());
          if (body.include_usage) { json usage = {{"id", completion_id}, {"object", "chat.completion.chunk"}, {"created", created}, {"model", result.model}, {"choices", json::array()}, {"usage", result.usage ? result.usage->to_openai() : json(nullptr)}}; encoded = sse_data(usage); sink.write(encoded.data(), encoded.size()); }
        } catch (const ProcessError& error) { auto encoded = sse_data(openai_error(error.kind == ProcessError::Kind::rate_limit ? "Provider subscription quota is exhausted" : "Provider stream failed", error.kind == ProcessError::Kind::rate_limit ? "rate_limit_error" : "server_error", error.kind == ProcessError::Kind::rate_limit ? "rate_limit_exceeded" : "stream_error")); sink.write(encoded.data(), encoded.size()); }
        static const std::string done = "data: [DONE]\n\n"; sink.write(done.data(), done.size()); sink.done(); return false;
      });
    } catch (const json::exception&) { error_response(request, response, Error("Invalid JSON body", 400, "validation_error"), id); } catch (const Error& error) { error_response(request, response, error, id); } catch (const std::exception&) { error_response(request, response, ProcessError("provider request failed"), id); }
  });
  server.Post("/v1/messages", [&](const httplib::Request& request, httplib::Response& response) {
    const auto id = response.get_header_value("X-Request-ID"); try { require_key(request, settings); auto original = json::parse(request.body); auto body = parse_anthropic_request(original); if (body.backend == "warm") throw Error("Warm Claude mode is unavailable because one stream-json process retains conversation state across turns.", 400, "invalid_request"); if (!registry.claude->accepts_model(body.model)) throw Error("Unknown or unapproved model", 400, "invalid_model", "model"); const auto message_id = request_id("msg_local_");
      if (!body.stream) { auto result = execute(registry, "claude", body); json content; std::string reason; if (!result.tool_calls.empty()) { auto tool = result.tool_calls[0]; content = json::array({{{"type", "tool_use"}, {"id", tool.id}, {"name", tool.name}, {"input", json::parse(tool.arguments)}}}); reason = result.finish_reason == "length" ? "max_tokens" : "tool_use"; } else { content = json::array({{{"type", "text"}, {"text", result.text.value_or("")}}}); reason = result.finish_reason == "length" ? "max_tokens" : result.finish_reason == "stop" ? "stop_sequence" : "end_turn"; }
        json_response(response, 200, {{"id", message_id}, {"type", "message"}, {"role", "assistant"}, {"content", content}, {"model", result.model}, {"stop_reason", reason}, {"stop_sequence", result.stop_sequence ? json(*result.stop_sequence) : json(nullptr)}, {"usage", {{"input_tokens", result.usage ? result.usage->prompt_tokens : 0}, {"output_tokens", result.usage ? result.usage->completion_tokens : 0}}}}); return; }
      response.set_header("Cache-Control", "no-cache"); response.set_header("X-Accel-Buffering", "no"); response.set_chunked_content_provider("text/event-stream", [&, body, message_id, id](std::size_t, httplib::DataSink& sink) mutable { auto send = [&](std::string event, const json& payload) { auto frame = "event: " + event + "\ndata: " + json_compact(payload) + "\n\n"; return sink.write(frame.data(), frame.size()); };
        send("message_start", {{"type", "message_start"}, {"message", {{"id", message_id}, {"type", "message"}, {"role", "assistant"}, {"content", json::array()}, {"model", body.model}, {"stop_reason", nullptr}, {"stop_sequence", nullptr}, {"usage", {{"input_tokens", 0}, {"output_tokens", 0}}}}}}); bool started = false;
        StreamController control(body, [&](std::string_view text) { if (!started) { started = true; send("content_block_start", {{"type", "content_block_start"}, {"index", 0}, {"content_block", {{"type", "text"}, {"text", ""}}}}); } return send("content_block_delta", {{"type", "content_block_delta"}, {"index", 0}, {"delta", {{"type", "text_delta"}, {"text", text}}}}); });
        try { auto result = execute(registry, "claude", body, [&](std::string_view text) { return control.push(text); }); control.flush(); if (control.finish) { result.text = control.delivered; result.finish_reason = control.finish; result.stop_sequence = control.stop_sequence; }
          if (!result.tool_calls.empty()) { auto tool = result.tool_calls[0]; started = true; send("content_block_start", {{"type", "content_block_start"}, {"index", 0}, {"content_block", {{"type", "tool_use"}, {"id", tool.id}, {"name", tool.name}, {"input", json::object()}}}}); send("content_block_delta", {{"type", "content_block_delta"}, {"index", 0}, {"delta", {{"type", "input_json_delta"}, {"partial_json", tool.arguments}}}}); }
          else if (!started && result.text) { started = true; send("content_block_start", {{"type", "content_block_start"}, {"index", 0}, {"content_block", {{"type", "text"}, {"text", ""}}}}); send("content_block_delta", {{"type", "content_block_delta"}, {"index", 0}, {"delta", {{"type", "text_delta"}, {"text", *result.text}}}}); }
          if (started)
            send("content_block_stop", {{"type", "content_block_stop"}, {"index", 0}});
          std::string reason = result.finish_reason == "length" ? "max_tokens" : result.finish_reason == "stop" ? "stop_sequence" : !result.tool_calls.empty() ? "tool_use" : "end_turn"; send("message_delta", {{"type", "message_delta"}, {"delta", {{"stop_reason", reason}, {"stop_sequence", result.stop_sequence ? json(*result.stop_sequence) : json(nullptr)}}}, {"usage", {{"output_tokens", result.usage ? result.usage->completion_tokens : 0}}}}); send("message_stop", {{"type", "message_stop"}});
        } catch (const ProcessError& error) { send("error", anthropic_error(error.kind == ProcessError::Kind::rate_limit ? "Provider subscription quota is exhausted" : "Provider stream failed", error.kind == ProcessError::Kind::rate_limit ? "rate_limit_error" : "api_error", id)); }
        sink.done(); return false; });
    } catch (const json::exception&) { error_response(request, response, Error("Invalid JSON body", 400, "validation_error"), id); } catch (const Error& error) { error_response(request, response, error, id); } catch (const std::exception&) { error_response(request, response, ProcessError("provider request failed"), id); }
  });
  const auto static_root = asset_root() / "static";
  server.Get("/", [static_root](const httplib::Request&, httplib::Response& response) { response.set_content(read_file(static_root / "index.html"), "text/html; charset=utf-8"); });
  server.Get("/static/app.js", [static_root](const httplib::Request&, httplib::Response& response) { response.set_content(read_file(static_root / "app.js"), "text/javascript; charset=utf-8"); });
  server.Get("/static/styles.css", [static_root](const httplib::Request&, httplib::Response& response) { response.set_content(read_file(static_root / "styles.css"), "text/css; charset=utf-8"); });
  server.Get("/favicon.ico", [](const httplib::Request&, httplib::Response& response) { response.status = 204; });
  server.Get("/openapi.json", [](const httplib::Request&, httplib::Response& response) { json_response(response, 200, {{"openapi", "3.1.0"}, {"info", {{"title", "Kessel Local API"}, {"version", KESSEL_CPP_VERSION}}}, {"paths", {{"/health", {{"get", json::object()}}}, {"/v1/{provider}/models", {{"get", json::object()}}}, {"/v1/{provider}/chat/completions", {{"post", json::object()}}}, {"/v1/messages", {{"post", json::object()}}}}}}); });
  server.Get("/docs", [](const httplib::Request&, httplib::Response& response) { response.set_content("<!doctype html><title>Kessel API docs</title><h1>Kessel Local API</h1><p><a href='/openapi.json'>OpenAPI 3.1 document</a></p>", "text/html; charset=utf-8"); });
  std::cout << "Kessel C++ listening at http://" << settings.listen_host << ':' << settings.listen_port << std::endl;
  std::atomic<bool> listener_done = false;
  std::thread stop_watcher([&] {
    const auto stop_path = state_directory() / "stop.request";
    while (!listener_done) {
      if (fs::exists(stop_path)) { server.stop(); return; }
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
  });
  const bool listened = server.listen(settings.listen_host, settings.listen_port);
  listener_done = true; stop_watcher.join();
  std::error_code stop_error; fs::remove(state_directory() / "stop.request", stop_error);
  registry.close(); return listened ? 0 : 1;
}

}  // namespace kessel
