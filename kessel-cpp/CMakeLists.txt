cmake_minimum_required(VERSION 3.25)
project(kessel_cpp VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_POLICY_VERSION_MINIMUM 3.5 CACHE STRING "" FORCE)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

include(FetchContent)
set(HTTPLIB_REQUIRE_OPENSSL OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_ZLIB_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_NON_BLOCKING_GETADDRINFO OFF CACHE BOOL "" FORCE)
set(HTTPLIB_INSTALL OFF CACHE BOOL "" FORCE)
set(JSON_Install OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  httplib
  GIT_REPOSITORY https://github.com/yhirose/cpp-httplib.git
  GIT_TAG v0.56.0
  GIT_SHALLOW TRUE
)
FetchContent_Declare(
  nlohmann_json
  GIT_REPOSITORY https://github.com/nlohmann/json.git
  GIT_TAG v3.12.0
  GIT_SHALLOW TRUE
)
set(CPP_TIKTOKEN_INSTALL OFF CACHE BOOL "" FORCE)
set(CPP_TIKTOKEN_TESTING OFF CACHE BOOL "" FORCE)
set(PCRE2_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(PCRE2_BUILD_PCRE2GREP OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  cpp_tiktoken
  GIT_REPOSITORY https://github.com/gh-markt/cpp-tiktoken.git
  GIT_TAG 9323db528d52e48900c75ce197c3251085b18480
  GIT_SHALLOW TRUE
  GIT_SUBMODULES_RECURSE TRUE
)
FetchContent_MakeAvailable(httplib nlohmann_json cpp_tiktoken)
set_property(DIRECTORY "${cpp_tiktoken_SOURCE_DIR}" PROPERTY EXCLUDE_FROM_ALL TRUE)

add_executable(kessel-cpp
  src/main.cpp
  src/core.cpp
  src/process.cpp
  src/providers.cpp
  src/server.cpp
  src/cli.cpp
)
target_include_directories(kessel-cpp PRIVATE include)
target_link_libraries(kessel-cpp PRIVATE httplib::httplib nlohmann_json::nlohmann_json tiktoken)
target_compile_definitions(kessel-cpp PRIVATE
  KESSEL_CPP_VERSION="${PROJECT_VERSION}"
  KESSEL_CPP_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
)
if(MSVC)
  target_compile_options(kessel-cpp PRIVATE /W4 /permissive-)
else()
  target_compile_options(kessel-cpp PRIVATE -Wall -Wextra -Wpedantic)
endif()
if(WIN32)
  target_compile_definitions(kessel-cpp PRIVATE _WIN32_WINNT=0x0A00 WINVER=0x0A00)
  target_link_libraries(kessel-cpp PRIVATE bcrypt ws2_32)
endif()

add_executable(kessel-mock-provider tests/mock_provider.cpp)
target_link_libraries(kessel-mock-provider PRIVATE nlohmann_json::nlohmann_json)
target_compile_features(kessel-mock-provider PRIVATE cxx_std_20)
if(MINGW)
  target_link_options(kessel-cpp PRIVATE -static-libgcc -static-libstdc++)
endif()
add_custom_command(TARGET kessel-cpp POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_FILE_DIR:kessel-cpp>/tokenizers"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${cpp_tiktoken_SOURCE_DIR}/o200k_base.tiktoken"
    "$<TARGET_FILE_DIR:kessel-cpp>/tokenizers/o200k_base.tiktoken"
)
file(GLOB KESSEL_STATIC_FILES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/static/*")
file(GLOB KESSEL_RESOURCE_FILES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/resources/*")
add_custom_target(kessel_assets ALL
  COMMAND ${CMAKE_COMMAND} -E copy_directory
    "${CMAKE_CURRENT_SOURCE_DIR}/static"
    "$<TARGET_FILE_DIR:kessel-cpp>/static"
  COMMAND ${CMAKE_COMMAND} -E copy_directory
    "${CMAKE_CURRENT_SOURCE_DIR}/resources"
    "$<TARGET_FILE_DIR:kessel-cpp>/resources"
  DEPENDS ${KESSEL_STATIC_FILES} ${KESSEL_RESOURCE_FILES}
)
add_dependencies(kessel-cpp kessel_assets)

install(TARGETS kessel-cpp RUNTIME DESTINATION bin)
install(DIRECTORY static resources DESTINATION bin)
install(FILES "${cpp_tiktoken_SOURCE_DIR}/o200k_base.tiktoken"
        DESTINATION bin/tokenizers)

include(CTest)
if(BUILD_TESTING)
  add_test(NAME kessel_cpp_self_test COMMAND kessel-cpp self-test)
  set_tests_properties(kessel_cpp_self_test PROPERTIES
    ENVIRONMENT "KESSEL_CONFIG_DIR=${CMAKE_BINARY_DIR}/test-state/config;KESSEL_STATE_DIR=${CMAKE_BINARY_DIR}/test-state/state"
  )
  if(WIN32)
    find_program(KESSEL_POWERSHELL powershell REQUIRED)
    add_test(
      NAME kessel_cpp_cli_availability
      COMMAND "${KESSEL_POWERSHELL}" -NoProfile -ExecutionPolicy Bypass
              -File "${CMAKE_CURRENT_SOURCE_DIR}/tests/cli-availability.ps1"
              -Binary "$<TARGET_FILE:kessel-cpp>"
              -MockProvider "$<TARGET_FILE:kessel-mock-provider>"
              -TestRoot "${CMAKE_BINARY_DIR}/cli-availability-state"
    )
    set_tests_properties(kessel_cpp_cli_availability PROPERTIES RUN_SERIAL TRUE)
  endif()
endif()
