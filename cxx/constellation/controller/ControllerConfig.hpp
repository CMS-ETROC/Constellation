/**
 * @file
 * @brief Configuration parser class
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <string>

#include "constellation/build.hpp"
#include "constellation/core/config/Dictionary.hpp"
#include "constellation/core/log/Logger.hpp"
#include "constellation/core/utils/string.hpp"
#include "constellation/core/utils/string_hash_map.hpp"

namespace constellation::controller {

    /**
     * @brief Configuration parser to read TOML files and emit dictionaries for individual satellites
     *
     * The configuration file holds a hierarchy of tables which contain the configuration keys for all satellites of the
     * Constellation. The dictionaries for the individual satellites need to be assembled from keys specific to the
     * respective satellite, keys valid for the relevant satellite type and keys intended for all satellites.
     */
    class CNSTLN_API ControllerConfiguration {
    public:
        virtual ~ControllerConfiguration() = default;

        // Empty config
        ControllerConfiguration() = default;

        // Parse from string
        explicit ControllerConfiguration(std::string_view);

        // Parse from file
        explicit ControllerConfiguration(const std::filesystem::path& path);

        /// @cond doxygen_suppress
        ControllerConfiguration(const ControllerConfiguration& other) = delete;
        ControllerConfiguration& operator=(const ControllerConfiguration& other) = delete;
        ControllerConfiguration(ControllerConfiguration&& other) noexcept = delete;
        ControllerConfiguration& operator=(ControllerConfiguration&& other) = delete;
        /// @endcond

        /**
         * @brief Parse configuration and prepare configuration dictionary for a given satellite
         *
         * The TOML parse tree is specifically searched for the given satellite and its type because the TOML format is
         * case-sensitive and we need insensitive matches.
         *
         * @param satellite Satellite to obtain configuration dictionary for
         * @param toml String view of the TOML configuration file contents
         * @return Optional with configuration dictionary if satellite was found in configuration
         * @throws ConfigFileNotFoundError if the configuration file could not be found or opened
         * @throws ConfigFileParseError if the configuration file could not be parsed into valid TOML
         * @throws ConfigFileTypeError if the configuration file contained invalid value types
         */
        std::optional<config::Dictionary> getSatelliteConfiguration(std::string_view canonical_name) const;

        /**
         * @brief Parse configuration and prepare configuration dictionary for a set of satellites
         *
         * It is necessary to also provide the set of satellites to parse this configuration for, since the TOML parse tree
         * is specifically searched for those satellites and types because the TOML format is case-sensitive and we need
         * insensitive matches.
         *
         * @param satellites Satellite to obtain configuration dictionary for
         * @param toml String view of the TOML configuration file contents
         * @return Map of all requested satellites that were found in the configuration
         * @throws ConfigFileNotFoundError if the configuration file could not be found or opened
         * @throws ConfigFileParseError if the configuration file could not be parsed into valid TOML
         * @throws ConfigFileTypeError if the configuration file contained invalid value types
         * @throws std::invalid_argument if no dictionary is available for this satellite or config parsing issues
         */
        std::map<std::string, config::Dictionary> getSatelliteConfigurations(std::set<std::string> canonical_names) const;

    private:
        // KVPs in global section
        config::Dictionary global_config_;

        // KVPs in type sections
        utils::string_hash_map<config::Dictionary> type_configs_;

        // KVPs in satellite-specific sections
        utils::string_hash_map<config::Dictionary> satellite_configs_;

        /* Logger */
        log::Logger config_parser_logger_ {"CFGPARSER"};
    };

} // namespace constellation::controller
