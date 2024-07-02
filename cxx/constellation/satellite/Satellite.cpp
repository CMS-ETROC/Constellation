/**
 * @file
 * @brief Implementation of Satellite class
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include "Satellite.hpp"

#include <stop_token>
#include <string_view>

#include "constellation/core/config/Configuration.hpp"
#include "constellation/core/logging/log.hpp"
#include "constellation/core/utils/string.hpp"
#include "constellation/satellite/BaseSatellite.hpp"
#include "constellation/satellite/fsm_definitions.hpp"

using namespace constellation::satellite;
using namespace constellation::utils;

Satellite::Satellite(std::string_view type, std::string_view name) : BaseSatellite(type, name) {}

void Satellite::initializing(config::Configuration& /* config */) {
    LOG(INFO) << "Initializing - default";
}

void Satellite::launching() {
    LOG(INFO) << "Launching - default";
}

void Satellite::landing() {
    LOG(INFO) << "Landing - default";
}

void Satellite::reconfiguring(const config::Configuration& /* partial_config */) {
    LOG(INFO) << "Reconfiguring - default";
}

void Satellite::starting(std::string_view run_identifier) {
    LOG(INFO) << "Starting run " << run_identifier << " - default";
}

void Satellite::stopping() {
    LOG(INFO) << "Stopping - default";
}

void Satellite::running(const std::stop_token& /* stop_token */) {
    LOG(INFO) << "Running - default";
}

void Satellite::interrupting(State previous_state) {
    LOG(INFO) << "Interrupting from " << to_string(previous_state) << " - default";
    if(previous_state == State::RUN) {
        LOG(logger_, DEBUG) << "Interrupting: execute stopping";
        stopping();
    }
    LOG(logger_, DEBUG) << "Interrupting: execute landing";
    landing();
}

void Satellite::onFailure(State previous_state) {
    LOG(INFO) << "Failure from " << to_string(previous_state) << " - default";
}
