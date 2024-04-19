/**
 * @file
 * @brief CHP heartbeat sender
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include <iostream>
#include <string>

#include "constellation/core/chirp/Manager.hpp"
#include "constellation/core/heartbeat/HeartbeatRecv.hpp"
#include "constellation/core/logging/Logger.hpp"

using namespace constellation;
using namespace constellation::heartbeat;
using namespace constellation::log;
using namespace constellation::message;
using namespace std::literals::chrono_literals;

// Use global std::function to work around C linkage
std::function<void(int)> signal_handler_f {}; // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

extern "C" void signal_hander(int signal) {
    signal_handler_f(signal);
}

int main(int argc, char* argv[]) {
    // Get address via cmdline
    if(argc != 2) {
        std::cout << "Invalid usage: chp_receiver CONSTELLATION_GROUP" << std::endl;
    }

    auto chirp_manager = chirp::Manager("255.255.255.255", "0.0.0.0", argv[1], "chp_receiver");
    chirp_manager.setAsDefaultInstance();
    chirp_manager.start();

    Logger logger {"chp_receiver"};

    HeartbeatRecv receiver {[&](const CHP1Message& msg) {
        LOG(logger, DEBUG) << msg.getSender() << " reports state " << magic_enum::enum_name(msg.getState())
                           << ", next message in " << msg.getInterval().count();
    }};

    auto receiver_thread = std::jthread(std::bind_front(&HeartbeatRecv::loop, &receiver));

    std::once_flag shut_down_flag {};
    signal_handler_f = [&](int /*signal*/) -> void {
        std::call_once(shut_down_flag, [&]() { receiver_thread.request_stop(); });
    };
    // NOLINTBEGIN(cert-err33-c)
    std::signal(SIGTERM, &signal_hander);
    std::signal(SIGINT, &signal_hander);
    // NOLINTEND(cert-err33-c)

    if(receiver_thread.joinable()) {
        receiver_thread.join();
    }

    return 0;
}
