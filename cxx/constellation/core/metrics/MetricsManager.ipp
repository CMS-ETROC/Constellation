/**
 * @file
 * @brief Metrics manager template members
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include "MetricsManager.hpp" // NOLINT(misc-header-include-cycle)

#include <chrono>
#include <concepts>
#include <functional>
#include <initializer_list>
#include <memory>
#include <string>
#include <type_traits>
#include <typeinfo>
#include <utility>

#include "constellation/core/config/Value.hpp"
#include "constellation/core/metrics/exceptions.hpp"
#include "constellation/core/metrics/Metric.hpp"
#include "constellation/core/protocol/CSCP_definitions.hpp"
#include "constellation/core/utils/type.hpp"

namespace constellation::metrics {

    inline void MetricsManager::registerMetric(std::string name, std::string unit, metrics::MetricType type) {
        registerMetric(std::make_shared<metrics::Metric>(std::move(name), std::move(unit), type));
    };

    template <typename C>
        requires std::invocable<C>
    void MetricsManager::registerTimedMetric(std::string name,
                                             std::string unit,
                                             metrics::MetricType type,
                                             std::chrono::steady_clock::duration interval,
                                             C value_callback,
                                             std::initializer_list<protocol::CSCP::State> allowed_states) {
        std::function<config::Value()> value_callback_cast =
            [name, value_callback = std::move(value_callback)]() -> config::Value {
            try {
                return config::Value::set(value_callback());
            } catch(const std::bad_cast&) {
                throw InvalidMetricValueException(name, utils::demangle<std::invoke_result_t<C>>());
            }
        };
        registerTimedMetric(std::make_shared<TimedMetric>(
            std::move(name), std::move(unit), type, interval, std::move(value_callback_cast), allowed_states));
    }

} // namespace constellation::metrics
