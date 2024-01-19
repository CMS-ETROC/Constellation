/**
 * @file
 * @brief Log Sink Manager
 *
 * @copyright Copyright (c) 2023 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <spdlog/async_logger.h>
#include <spdlog/sinks/stdout_color_sinks.h>

#include "constellation/core/config.hpp"
#include "constellation/core/logging/CMDP1Sink.hpp"
#include "constellation/core/logging/Level.hpp"

namespace constellation::log {
    /**
     * Global sink manager
     *
     * This class manager the console and CMDP1 sinks and can creates new spdlog loggers.
     */
    class SinkManager {
    public:
        CNSTLN_API static SinkManager& getInstance();

        SinkManager(SinkManager const&) = delete;
        SinkManager& operator=(SinkManager const&) = delete;
        SinkManager(SinkManager&&) = default;
        SinkManager& operator=(SinkManager&&) = default;
        ~SinkManager() = default;

        /**
         * Set the global (default) console log level
         *
         * @param level Log level for console output
         */
        CNSTLN_API void setGlobalConsoleLevel(Level level);

        /**
         * Create a new asynchronous spglog logger
         *
         * @param topic Topic of the new logger
         * @param console_level Optional log level for console output to overwrite global level
         * @return Shared pointer to the new logger
         */
        CNSTLN_API std::shared_ptr<spdlog::async_logger> createLogger(std::string topic,
                                                                      std::optional<Level> console_level = std::nullopt);

        // TODO(stephan.lachnit): remove, this debug until fetching subscriptions from ZeroMQ is implemented
        CNSTLN_API void setCMDPLevelsCustom(Level cmdp_global_level,
                                            std::map<std::string_view, Level> cmdp_sub_topic_levels = {});

    private:
        SinkManager();

        /**
         * Set the CMDP log level for a particular logger given the current subscriptions
         *
         * @param logger Logger for which to set the log level
         */
        void setCMDPLevel(std::shared_ptr<spdlog::async_logger>& logger);

    private:
        std::shared_ptr<spdlog::sinks::stdout_color_sink_mt> console_sink_;
        std::shared_ptr<CMDP1Sink> cmdp1_sink_;

        std::vector<std::shared_ptr<spdlog::async_logger>> loggers_;

        Level cmdp_global_level_;
        std::map<std::string_view, Level> cmdp_sub_topic_levels_;
    };
} // namespace constellation::log
