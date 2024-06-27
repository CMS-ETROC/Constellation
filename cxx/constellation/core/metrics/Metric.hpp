/**
 * @file
 * @brief Metric class
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <variant>

#include "constellation/build.hpp"
#include "constellation/core/config/Value.hpp"
#include "constellation/core/message/PayloadBuffer.hpp"

namespace constellation::metrics {

    /** Metrics types */
    enum class Type : std::uint8_t {
        /** Always keep the latest value, replace earlier ones */
        LAST_VALUE = 1,
        /** Sum every new value to previously received ones */
        ACCUMULATE = 2,
        /** Calculate the average value */
        AVERAGE = 3,
        /** Calculate the rate from the value over a given time interval */
        RATE = 4,
    };
    using enum Type;

    /**
     * @class Metric
     * @brief This class represents a metric for data quality monitoring or statistics purposes.
     *
     *  @details It comprises a value, a unit and a type. The type defines how the value should be treated, i.e. if always
     *  the last transmitted value should be displayed, if an average of the values should be calculated or if they should
     *  be accumulated.
     */
    class CNSTLN_API Metric {
    public:
        /**
         * @brief Metrics constructor
         *
         * @param unit Unit of this metric as human readable string
         * @param type Type of the metric
         * @param initial_value Initial value
         */
        Metric(std::string unit, Type type, config::Value&& initial_value)
            : unit_(std::move(unit)), type_(type), value_(std::move(initial_value)) {}

        Metric() : type_(LAST_VALUE), value_(std::monostate {}) {};
        virtual ~Metric() noexcept = default;

        // Default copy/move constructor/assignment
        Metric(const Metric&) = default;
        Metric& operator=(const Metric&) = default;
        Metric(Metric&&) noexcept = default;
        Metric& operator=(Metric&&) = default;

        /** Setting or updating the value */
        void set(config::Value&& value) { value_ = std::move(value); }

        /**
         * @brief Obtain current metric value
         * @return Current value
         */
        template <typename T> T value() const { return value_.get<T>(); }

        /**
         * @brief Obtain unit as human-readable string
         * @return Unit of the metric
         */
        std::string_view unit() const { return unit_; }

        /**
         * @brief Obtain type of the metric
         * @return Metric type
         */
        Type type() const { return type_; }

        /** Assemble metric via msgpack for message payload */
        message::PayloadBuffer assemble() const;

        /** Disassemble metric from message payload */
        static Metric disassemble(const message::PayloadBuffer& message);

    private:
        std::string unit_;
        Type type_;
        config::Value value_ {};
    };

    using Clock = std::chrono::high_resolution_clock;

    /**
     * @class MetricTimer
     * @brief Helper class for the controlled emission of the metric
     * @details This class provides check on whether the value has changed or the condition for an distribution is met. The
     * condition method is purely virtual and needs to be implemented by derived timers with a specific behavior. This class
     * also allows to limit the distribution of the metric to certain states of the FSM.
     */
    class MetricTimer : public Metric {
    public:
        /**
         * @brief MetricTimer constructor
         *
         * @param unit Unit of the metric
         * @param type Type of the metric
         * @param states List of states in which this metric should be distributed
         * @param value Initial metric value
         */
        MetricTimer(std::string_view unit,
                    const Type type,
                    std::initializer_list<message::State> states,
                    const config::Value& value = {})
            : Metric(unit, type, value), states_(states) {}

        /**
         * @brief Checks if this metric should be distributed now
         * @details This method checks if the value has changed since the last distribution, if the current state matches the
         * states in which the metric should be distributed, and if the condition for distribution is fulfilled.
         *
         * @param state Current state of the FSM
         * @return True if the metric should be sent now, false otherwise
         */
        bool check(message::State state);

        /**
         * @brief Helper to check when the next time of distribution is expected
         * @details This method can overwritten by derived timer classes to give an estimate when their next distribution
         * condition is met. This helps the sending thread to sleep until the next metric is sent
         * @return Time point in the future when the next metric distribution is expected
         */
        virtual Clock::time_point nextTrigger() const { return Clock::time_point::max(); }

        /**
         * @brief Update method for the metric value
         * @details This sets the new value of the metric and marks it as changed if the new value is different from the old
         *
         * @param value Metric value
         */
        virtual void update(const config::Value& value);

    protected:
        /**
         * @brief Purely virtual method to be overwritten by derived classes for defining distribution conditions
         * @details Derived classes can use this interface to define conditions such as a specific time interval or a number
         * of updates of the metric before it is sent.
         * @return True if the condition is met, false otherwise
         */
        virtual bool condition() = 0;

    private:
        bool changed_ {true};
        std::set<message::State> states_;
    };

    /**
     * @class TimedMetric
     * @brief Metric timer to send metric values in regular intervals
     * @details This timer is configured with a time interval, and metrics will be sent every time this interval has passed.
     */
    class TimedMetric : public MetricTimer {
    public:
        TimedMetric(std::string_view unit,
                    Type type,
                    Clock::duration interval,
                    std::initializer_list<message::State> states,
                    const config::Value& value = {})
            : MetricTimer(unit, type, states, value), interval_(interval), last_trigger_(Clock::now()),
              last_check_(Clock::now()) {}

        bool condition() override;
        Clock::time_point nextTrigger() const override;

    private:
        Clock::duration interval_;
        Clock::time_point last_trigger_;
        Clock::time_point last_check_;
    };

    /**
     * @class TriggeredMetric
     * @brief Metric timer so send metric values after N updates or update attempts
     * @details This timer is configured with a trigger number, and the metric is distributed every time the update method of
     * this timer has been called N times. This can be useful to e.g. send a metric for data quality monitoring not at
     * regular time intervals but after every 100 data recordings.
     */
    class TriggeredMetric : public MetricTimer {
    public:
        TriggeredMetric(std::string_view unit,
                        Type type,
                        std::size_t triggers,
                        std::initializer_list<message::State> states,
                        const config::Value& value);

        void update(const config::Value& value) override;

        bool condition() override;

    private:
        std::size_t triggers_;
        std::size_t current_triggers_ {0};
    };
} // namespace constellation::metrics
