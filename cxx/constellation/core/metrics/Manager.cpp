/**
 * @file
 * @brief Metrics Manager
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include "Manager.hpp"

#include <chrono>
#include <condition_variable>
#include <map>
#include <thread>

#include "constellation/core/message/CMDP1Message.hpp"

using namespace constellation::message;
using namespace constellation::metrics;

void Manager::Metric::set(const config::Value& value) {
    if(value != value_) {
        value_ = value;
        changed_ = true;
    }
}

bool Manager::Metric::check() {
    if(!changed_) {
        return false;
    }
    if(condition()) {
        changed_ = false;
        return true;
    }
    return false;
}

bool Manager::TimedMetric::condition() {

    auto duration = Clock::now() - last_trigger_;

    if(duration >= interval_) {
        last_trigger_ += interval_;
        return true;
    }

    return false;
}

Clock::time_point Manager::TimedMetric::next_trigger() const {
    return last_trigger_ + interval_;
}
Manager::TriggeredMetric::TriggeredMetric(const std::size_t triggers, const Type type, config::Value value)
    : Metric(type, value), triggers_(triggers) {
    // We have an initial value, let's log it directly
    if(!std::holds_alternative<std::monostate>(value)) {
        current_triggers_ = triggers_;
    }
}

void Manager::TriggeredMetric::set(const config::Value& value) {
    Metric::set(value);
    current_triggers_++;
}

bool Manager::TriggeredMetric::condition() {

    if(current_triggers_ >= triggers_) {
        current_triggers_ = 0;
        return true;
    }

    return false;
}

Manager* Manager::getDefaultInstance() {
    return Manager::default_manager_instance_;
}

void Manager::setAsDefaultInstance() {
    Manager::default_manager_instance_ = this;
}

Manager::~Manager() noexcept {
    thread_.request_stop();
    cv_.notify_all();

    if(thread_.joinable()) {
        thread_.join();
    }
}

void Manager::setMetric(const std::string& topic, config::Value value) {
    // FIXME access
    metrics_.at(topic)->set(value);

    // Notify if this is a triggered metric:
    if(std::dynamic_pointer_cast<TriggeredMetric>(metrics_.at(topic))) {
        cv_.notify_all();
    }
}

void Manager::unregisterMetric(std::string topic) {
    metrics_.erase(topic);
}

bool Manager::registerTriggeredMetric(const std::string& topic, std::size_t triggers, Type type, config::Value value) {
    std::unique_lock<std::mutex> lock {mt_};
    metrics_[topic] = std::make_shared<TriggeredMetric>(triggers, type, value);
    cv_.notify_all();
    return true;
}

bool Manager::registerTimedMetric(const std::string& topic, Clock::duration interval, Type type, config::Value value) {
    std::unique_lock<std::mutex> lock {mt_};
    metrics_[topic] = std::make_shared<TimedMetric>(interval, type, value);
    cv_.notify_all();
    return true;
}

void Manager::run(const std::stop_token& stop_token) {
    std::unique_lock<std::mutex> lock {mt_};

    while(!stop_token.stop_requested()) {

        auto next = Clock::time_point::max();
        for(auto& [key, timer] : metrics_) {
            if(timer->check()) {
                // Create message header
                auto msghead = CMDP1Message::Header("test", Clock::now());

                // Create and send CMDP message
                auto msg = CMDP1StatMessage(key, std::move(msghead), timer->value(), timer->type()).assemble();

                // FIXME .send(publisher)
            }

            // Update time point until we can wait:
            next = std::min(next, timer->next_trigger());
        }

        cv_.wait_until(lock, next, [&]() { return stop_token.stop_requested(); });
    }
}
