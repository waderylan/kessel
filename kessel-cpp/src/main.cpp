#include "kessel/core.hpp"

#include <iostream>

int main(int argc, char** argv) {
  try {
    return kessel::run_cli(argc, argv);
  } catch (const kessel::Error& error) {
    std::cerr << error.what() << '\n';
    return error.status > 0 && error.status < 256 ? error.status : 1;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}

