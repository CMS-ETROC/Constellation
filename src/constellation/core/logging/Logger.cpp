/**
 * @file
 * @brief Implementation of Logger
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include "Logger.hpp"

#include "constellation/core/logging/SinkManager.hpp"

using namespace constellation::log;

Logger::Logger(std::string topic) : topic_(std::move(topic)) {
    // Create logger from global sinks
    spdlog_logger_ = SinkManager::getInstance().createLogger(topic_);
}

void Logger::setConsoleLogLevel(Level level) {
    // The logger itself forwards all debug messages to sinks by default,
    // console output controlled by the corresponding sink
    SinkManager::getInstance().getConsoleSink()->set_level(to_spdlog_level(level));
}

// Enables backtrace and enables TRACE messages over ZeroMQ sink
void Logger::enableTrace(bool enable) {
    if(enable) {
        spdlog_logger_->set_level(spdlog::level::level_enum::trace);
        spdlog_logger_->enable_backtrace(BACKTRACE_MESSAGES);
    } else {
        spdlog_logger_->set_level(spdlog::level::level_enum::debug);
        spdlog_logger_->disable_backtrace();
    }
}

bool Logger::shouldLog(Level level) const {
    return spdlog_logger_->should_log(to_spdlog_level(level));
}

swap_ostringstream Logger::log(Level level, std::source_location src_loc) {
    os_level_ = level;
    source_loc_ = src_loc;
    return {this};
}

void Logger::log(Level level, std::string_view message) {
    spdlog_logger_->log(to_spdlog_level(level), message);
}

void Logger::flush() {
    // Actually execute logging, needs string copy since this might be async
    spdlog_logger_->log({source_loc_.file_name(), static_cast<int>(source_loc_.line()), source_loc_.function_name()},
                        to_spdlog_level(os_level_),
                        os_.str());

    // Clear the stream by creating a new one
    os_ = std::ostringstream();
}
