/**
 * @file
 * @brief Metrics Manager
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include "MetricsManager.hpp"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <iomanip>
#include <memory>
#include <mutex>
#include <stop_token>
#include <string>
#include <thread>
#include <utility>

#include "constellation/core/config/Value.hpp"
#include "constellation/core/log/log.hpp"
#include "constellation/core/log/SinkManager.hpp"
#include "constellation/core/metrics/Metric.hpp"

using namespace constellation::config;
using namespace constellation::log;
using namespace constellation::message;
using namespace constellation::metrics;
using namespace std::chrono_literals;

MetricsManager::MetricsManager() : logger_("STAT"), thread_(std::bind_front(&MetricsManager::run, this)) {};

MetricsManager::~MetricsManager() noexcept {
    thread_.request_stop();
    cv_.notify_one();

    if(thread_.joinable()) {
        thread_.join();
    }
}

void MetricsManager::registerMetric(std::shared_ptr<Metric> metric) {
    const auto name = std::string(metric->name());

    std::unique_lock metrics_lock {metrics_mutex_};
    const auto [it, inserted] = metrics_.insert_or_assign(name, std::move(metric));
    metrics_lock.unlock();

    if(!inserted) {
        // Erase from timed metrics map in case previously registered as timed metric
        std::unique_lock timed_metrics_lock {timed_metrics_mutex_};
        timed_metrics_.erase(name);
        timed_metrics_lock.unlock();
        LOG(logger_, DEBUG) << "Replaced already registered metric " << std::quoted(name);
    }

    LOG(logger_, DEBUG) << "Successfully registered metric " << std::quoted(name);
}

void MetricsManager::registerTimedMetric(std::shared_ptr<TimedMetric> metric) {
    const auto name = std::string(metric->name());

    // Add to metrics map
    std::unique_lock metrics_lock {metrics_mutex_};
    const auto [it, inserted] = metrics_.insert_or_assign(name, metric);
    metrics_lock.unlock();

    if(!inserted) {
        LOG(logger_, DEBUG) << "Replaced already registered metric " << std::quoted(name);
    }

    // Now also add to timed metrics map
    std::unique_lock timed_metrics_lock {timed_metrics_mutex_};
    timed_metrics_.insert_or_assign(name, TimedMetricEntry(std::move(metric), std::chrono::steady_clock::time_point()));
    timed_metrics_lock.unlock();

    LOG(logger_, DEBUG) << "Successfully registered timed metric " << std::quoted(name);
}

void MetricsManager::unregisterMetric(std::string_view name) {
    std::unique_lock metrics_lock {metrics_mutex_};
    auto it = metrics_.find(name);
    if(it != metrics_.end()) {
        metrics_.erase(it);
    }
    metrics_lock.unlock();

    std::unique_lock timed_metrics_lock {timed_metrics_mutex_};
    auto it_timed = timed_metrics_.find(name);
    if(it_timed != timed_metrics_.end()) {
        timed_metrics_.erase(it_timed);
    }
    timed_metrics_lock.unlock();
}

void MetricsManager::unregisterMetrics() {
    std::unique_lock metrics_lock {metrics_mutex_};
    metrics_.clear();
    metrics_lock.unlock();

    std::unique_lock timed_metrics_lock {timed_metrics_mutex_};
    timed_metrics_.clear();
    timed_metrics_lock.unlock();
}

void MetricsManager::triggerMetric(std::string name, Value value) {
    // Only emplace name and value, do the lookup in the run thread
    std::unique_lock triggered_queue_lock {triggered_queue_mutex_};
    triggered_queue_.emplace(std::move(name), std::move(value));
    triggered_queue_lock.unlock();
    cv_.notify_one();
}

void MetricsManager::run(const std::stop_token& stop_token) {
    LOG(logger_, TRACE) << "Started metric dispatch thread";

    // Notify condition variable when stop is requested
    const std::stop_callback stop_callback {stop_token, [&]() { cv_.notify_one(); }};

    auto wakeup = std::chrono::steady_clock::now() + 1s;

    while(!stop_token.stop_requested()) {
        std::unique_lock triggered_queue_lock {triggered_queue_mutex_};
        // Wait until condition variable is notified or timeout is reached
        cv_.wait_until(triggered_queue_lock, wakeup);

        // Send any triggered metrics in the queue
        while(!triggered_queue_.empty()) {
            auto [name, value] = std::move(triggered_queue_.front());
            triggered_queue_.pop();
            LOG(logger_, TRACE) << "Looking for metric " << std::quoted(name);
            const std::lock_guard metrics_lock {metrics_mutex_};
            auto metric_it = metrics_.find(name);
            if(metric_it != metrics_.end()) {
                SinkManager::getInstance().sendCMDPMetric({metric_it->second, std::move(value)});
            } else {
                LOG(logger_, WARNING) << "Metric " << std::quoted(name) << " is not registered";
            }
        }
        triggered_queue_lock.unlock();

        // Set next wakeup to 1s from now
        const auto now = std::chrono::steady_clock::now();
        wakeup = now + 1s;

        // Check timed metrics
        const std::lock_guard timed_metrics_lock {timed_metrics_mutex_};
        for(auto& [name, timed_metric] : timed_metrics_) {
            // If last time sent larger than interval and allowed -> send metric
            if(now - timed_metric.last_sent > timed_metric.metric->interval()) {
                SinkManager::getInstance().sendCMDPMetric({timed_metric.metric, timed_metric.metric->currentValue()});
                timed_metric.last_sent = now;
            }
            // Update time point until we have to wait (if not in the past)
            const auto next_trigger = timed_metric.last_sent + timed_metric.metric->interval();
            if(next_trigger - now > std::chrono::steady_clock::duration::zero()) {
                wakeup = std::min(wakeup, next_trigger);
            }
        }
    }
}
