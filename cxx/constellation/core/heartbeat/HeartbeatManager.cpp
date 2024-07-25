/**
 * @file
 * @brief Implementation of the heartbeat manager
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include "HeartbeatManager.hpp"

#include <algorithm>
#include <chrono>
#include <functional>
#include <iterator>
#include <mutex>
#include <optional>
#include <string>
#include <utility>

#include "constellation/core/log/log.hpp"
#include "constellation/core/message/exceptions.hpp"
#include "constellation/core/protocol/CSCP_definitions.hpp"
#include "constellation/core/utils/std_future.hpp"
#include "constellation/core/utils/string.hpp"

using namespace constellation::heartbeat;
using namespace constellation::message;
using namespace constellation::utils;
using namespace constellation::protocol;
using namespace std::literals::chrono_literals;

HeartbeatManager::HeartbeatManager(std::string sender,
                                   std::function<CSCP::State()> state_callback,
                                   std::function<void(std::string_view)> interrupt_callback)
    : receiver_([this](auto&& arg) { process_heartbeat(std::forward<decltype(arg)>(arg)); }),
      sender_(std::move(sender), std::move(state_callback), 5000ms), interrupt_callback_(std::move(interrupt_callback)),
      logger_("CHP"), watchdog_thread_(std::bind_front(&HeartbeatManager::run, this)) {}

HeartbeatManager::~HeartbeatManager() {
    watchdog_thread_.request_stop();
    if(watchdog_thread_.joinable()) {
        watchdog_thread_.join();
    }
}

void HeartbeatManager::sendExtrasystole() {
    sender_.sendExtrasystole();
}

std::optional<CSCP::State> HeartbeatManager::getRemoteState(const std::string& remote) {
    const auto remote_it = remotes_.find(remote);
    if(remote_it != remotes_.end()) {
        return remote_it->second.last_state;
    }

    // Remote unknown, return empty optional
    return {};
}

void HeartbeatManager::process_heartbeat(const CHP1Message& msg) {
    LOG(logger_, TRACE) << msg.getSender() << " reports state " << to_string(msg.getState()) << ", next message in "
                        << msg.getInterval();

    const auto now = std::chrono::system_clock::now();

    // Update or add the remote:
    const auto remote_it = remotes_.find(to_string(msg.getSender()));
    if(remote_it != remotes_.end()) {

        const auto deviation = std::chrono::duration_cast<std::chrono::seconds>(now - msg.getTime());
        if(std::chrono::abs(deviation) > 3s) [[unlikely]] {
            LOG(logger_, WARNING) << "Detected time deviation of " << deviation << " to " << msg.getSender();
        }

        remote_it->second.interval = msg.getInterval();
        remote_it->second.last_heartbeat = now;
        remote_it->second.last_state = msg.getState();

        // Replenish lives unless we're in ERROR or SAFE state:
        if(msg.getState() != CSCP::State::ERROR && msg.getState() != CSCP::State::SAFE) {
            remote_it->second.lives = protocol::CHP::Lives;
        }
    } else {
        remotes_.emplace(msg.getSender(), Remote(msg.getInterval(), now, msg.getState(), now));
    }
}

void HeartbeatManager::run(const std::stop_token& stop_token) {
    std::unique_lock<std::mutex> lock {mutex_};
    auto wakeup = std::chrono::system_clock::now() + 3s;

    // Wait until cv is notified, timeout is reached or stop is requested, returns true if stop requested
    while(!cv_.wait_until(lock, stop_token, wakeup, [&]() { return stop_token.stop_requested(); })) {

        // Calculate the next wake-up by checking when the next heartbeat times out, but time out after 3s anyway:
        wakeup = std::chrono::system_clock::now() + 3s;
        for(auto& [key, remote] : remotes_) {
            // Check for ERROR and SAFE states:
            if(remote.lives > 0 && (remote.last_state == CSCP::State::ERROR || remote.last_state == CSCP::State::SAFE)) {
                remote.lives = 0;
                if(interrupt_callback_) {
                    LOG(logger_, DEBUG) << "Detected state " << to_string(remote.last_state) << " at " << key
                                        << ", interrupting";
                    interrupt_callback_(key + " reports state " + to_string(remote.last_state));
                }
            }

            // Check if we are beyond the interval and that we only subtract lives once every interval
            const auto now = std::chrono::system_clock::now();
            if(remote.lives > 0 && now - remote.last_heartbeat > remote.interval &&
               now - remote.last_checked > remote.interval) {
                // We have lives left, reduce them by one
                remote.lives--;
                remote.last_checked = now;
                LOG(logger_, TRACE) << "Missed heartbeat from " << key << ", reduced lives to " << to_string(remote.lives);

                if(remote.lives == 0 && interrupt_callback_) {
                    // This parrot is dead, it is no more
                    LOG(logger_, DEBUG) << "Missed heartbeats from " << key << ", no lives left";
                    interrupt_callback_("No signs of life detected anymore from " + key);
                }
            }

            // Update time point until we have to wait (if not in the past)
            const auto next_heartbeat = remote.last_heartbeat + remote.interval;
            if(next_heartbeat - now > std::chrono::system_clock::duration::zero()) {
                wakeup = std::min(wakeup, next_heartbeat);
            }
            LOG(logger_, TRACE) << "Updated heartbeat wakeup timer to "
                                << std::chrono::duration_cast<std::chrono::milliseconds>(wakeup - now);
        }
    }
}
