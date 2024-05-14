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

#include "constellation/core/logging/log.hpp"
#include "constellation/core/message/exceptions.hpp"
#include "constellation/core/utils/casts.hpp"

using namespace constellation::heartbeat;
using namespace constellation::message;
using namespace constellation::utils;
using namespace std::literals::chrono_literals;

HeartbeatManager::HeartbeatManager(std::string sender)
    : receiver_([this](auto&& arg) { process_heartbeat(std::forward<decltype(arg)>(arg)); }),
      sender_(std::move(sender), 1000ms), logger_("CHP"), watchdog_thread_(std::bind_front(&HeartbeatManager::run, this)) {}

HeartbeatManager::~HeartbeatManager() {
    watchdog_thread_.request_stop();
    cv_.notify_one();

    if(watchdog_thread_.joinable()) {
        watchdog_thread_.join();
    }
}

void HeartbeatManager::updateState(State state) {
    sender_.updateState(state);
}

std::optional<State> HeartbeatManager::getRemoteState(const std::string& remote) {
    const auto remote_it = remotes_.find(remote);
    if(remote_it != remotes_.end()) {
        return remote_it->second.last_state;
    }

    // Remote unknown, return empty optional
    return {};
}

void HeartbeatManager::process_heartbeat(const message::CHP1Message& msg) {
    LOG(logger_, TRACE) << msg.getSender() << " reports state " << to_string(msg.getState()) << ", next message in "
                        << msg.getInterval().count();

    const auto now = std::chrono::system_clock::now();

    // Update or add the remote:
    const auto remote_it = remotes_.find(to_string(msg.getSender()));
    if(remote_it != remotes_.end()) {

        if(now - msg.getTime() > 3s) [[unlikely]] {
            LOG(logger_, WARNING) << "Detected time deviation of "
                                  << std::chrono::duration_cast<std::chrono::milliseconds>(now - msg.getTime()).count()
                                  << "ms to " << msg.getSender();
        }

        remote_it->second.interval = msg.getInterval();
        remote_it->second.last_heartbeat = now;
        remote_it->second.last_state = msg.getState();

        // Replenish lives unless we're in ERROR or SAFE state:
        if(msg.getState() != State::ERROR && msg.getState() != State::SAFE) {
            remote_it->second.lives = 3;
        }
    } else {
        remotes_.emplace(msg.getSender(), Remote(msg.getInterval(), now, msg.getState()));
    }
}

void HeartbeatManager::run(const std::stop_token& stop_token) {
    std::unique_lock<std::mutex> lock {mutex_};

    while(!stop_token.stop_requested()) {

        // Calculate the next wake-up by checking when the next heartbeat times out, but time out after 3s anyway:
        auto wakeup = std::chrono::system_clock::now() + 3s;
        for(auto& [key, remote] : remotes_) {
            // Check for ERROR and SAFE states:
            if(remote.lives > 0 && (remote.last_state == State::ERROR || remote.last_state == State::SAFE)) {
                remote.lives = 0;
                if(interrupt_callback_) {
                    LOG(logger_, DEBUG) << "Detected state " << to_string(remote.last_state) << " at " << key
                                        << ", interrupting";
                    interrupt_callback_();
                }
            }

            // Check if we are beyond the interval
            if(remote.lives > 0 && std::chrono::system_clock::now() > remote.last_heartbeat + remote.interval) {
                // We have lives left, reduce them by one
                remote.lives--;
                // We have subtracted a live, so let's wait another interval:
                remote.last_heartbeat = std::chrono::system_clock::now();
                LOG(logger_, TRACE) << "Missed heartbeat from " << key << ", reduced lives to " << remote.lives;

                if(remote.lives == 0 && interrupt_callback_) {
                    // This parrot is dead, it is no more
                    LOG(logger_, DEBUG) << "Missed heartbeats from " << key << ", no lives left";
                    interrupt_callback_();
                }
            }

            // Update time point until we have to wait:
            wakeup = std::min(wakeup, remote.last_heartbeat + remote.interval);
        }

        cv_.wait_until(lock, wakeup);
    }
}
