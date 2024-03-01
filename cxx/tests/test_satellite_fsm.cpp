/**
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include <atomic>
#include <chrono>
#include <exception>
#include <memory>
#include <thread>

#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_string.hpp>
#include <zmq.hpp>

#include "constellation/core/message/CSCP1Message.hpp"
#include "constellation/satellite/FSM.hpp"
#include "constellation/satellite/fsm_definitions.hpp"
#include "constellation/satellite/Satellite.hpp"

using namespace Catch::Matchers;
using namespace constellation::satellite;
using namespace std::literals::chrono_literals;

// NOLINTNEXTLINE(*-special-member-functions)
class DummySatellite : public Satellite {
public:
    DummySatellite() { support_reconfigure(); }
    ~DummySatellite() override {
        logger_.flush();
        std::this_thread::sleep_for(1ms);
    }
    void dummy_support_reconfigure(bool support_reconfigure) { Satellite::support_reconfigure(support_reconfigure); }
    void dummy_throw_transitional() { throw_transitional_ = true; }
    void initializing(const std::stop_token& stop_token, const std::any& config) override {
        Satellite::initializing(stop_token, config);
        transitional_state(stop_token);
    }
    void launching(const std::stop_token& stop_token) override {
        Satellite::launching(stop_token);
        transitional_state(stop_token);
    }
    void landing(const std::stop_token& stop_token) override {
        Satellite::landing(stop_token);
        transitional_state(stop_token);
    }
    void reconfiguring(const std::stop_token& stop_token, const std::any& partial_config) override {
        Satellite::reconfiguring(stop_token, partial_config);
        transitional_state(stop_token);
    }
    void starting(const std::stop_token& stop_token, std::uint32_t run_number) override {
        Satellite::starting(stop_token, run_number);
        transitional_state(stop_token);
    }
    void stopping(const std::stop_token& stop_token) override {
        Satellite::stopping(stop_token);
        transitional_state(stop_token);
    }
    void running(const std::stop_token& stop_token) override {
        Satellite::running(stop_token);
        transitional_state(stop_token);
    }
    void interrupting(const std::stop_token& stop_token, State previous_state) override {
        Satellite::interrupting(stop_token, previous_state);
        transitional_state(stop_token);
    }
    void on_failure(const std::stop_token& stop_token, State previous_state) override {
        Satellite::on_failure(stop_token, previous_state);
        transitional_state(stop_token);
    }
    void progress_fsm(FSM& fsm) {
        auto old_state = fsm.getState();
        progress_fsm_ = true;
        // wait for state change
        while(old_state == fsm.getState()) {
            std::this_thread::sleep_for(10ms);
        }
        progress_fsm_ = false;
    }

private:
    void transitional_state(const std::stop_token& stop_token) {
        while(!progress_fsm_ && !stop_token.stop_requested()) {
            if(throw_transitional_) {
                throw_transitional_ = false;
                throw std::exception();
            }
        }
    }

private:
    std::atomic_bool progress_fsm_ {false};
    std::atomic_bool throw_transitional_ {false};
};

// NOLINTBEGIN(cert-err58-cpp,misc-use-anonymous-namespace)

TEST_CASE("Regular FSM operation", "[satellite][satellite::fsm]") {
    auto satellite = std::make_shared<DummySatellite>();
    auto fsm = FSM(satellite);

    // NEW -> INIT
    fsm.react(Transition::initialize);
    REQUIRE(fsm.getState() == State::initializing);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::INIT);
    // INIT -> INIT
    fsm.react(Transition::initialize);
    REQUIRE(fsm.getState() == State::initializing);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::INIT);
    // INIT -> ORBIT
    fsm.react(Transition::launch);
    REQUIRE(fsm.getState() == State::launching);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::ORBIT);
    // ORBIT -> ORBIT
    fsm.react(Transition::reconfigure);
    REQUIRE(fsm.getState() == State::reconfiguring);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::ORBIT);
    // ORBIT -> RUN
    fsm.react(Transition::start);
    REQUIRE(fsm.getState() == State::starting);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::RUN);
    // RUN -> ORBIT
    fsm.react(Transition::stop);
    REQUIRE(fsm.getState() == State::stopping);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::ORBIT);
    // ORBIT -> INT
    fsm.react(Transition::land);
    REQUIRE(fsm.getState() == State::landing);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::INIT);
}

TEST_CASE("FSM interrupts and failures", "[satellite][satellite::fsm]") {
    auto satellite = std::make_shared<DummySatellite>();
    auto fsm = FSM(satellite);

    // Failure in transitional state
    fsm.react(Transition::initialize);
    REQUIRE(fsm.getState() == State::initializing);
    satellite->dummy_throw_transitional();
    while(fsm.getState() == State::initializing) {
        std::this_thread::sleep_for(10ms);
    }
    REQUIRE(fsm.getState() == State::ERROR);

    // Failure on failure not allowed (use reactIfAllowed)
    REQUIRE_FALSE(fsm.isAllowed(Transition::failure));
    REQUIRE_FALSE(fsm.reactIfAllowed(Transition::failure));

    // Reset
    fsm.react(Transition::initialize);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::INIT);

    // Interrupt in transitional state
    fsm.react(Transition::launch);
    REQUIRE(fsm.getState() == State::launching);
    fsm.react(Transition::interrupt);
    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::SAFE);
}

TEST_CASE("React via CSCP", "[satellite][satellite::fsm][cscp]") {
    auto satellite = std::make_shared<DummySatellite>();
    auto fsm = FSM(satellite);
    using constellation::message::CSCP1Message;

    auto payload_frame = std::make_shared<zmq::message_t>();
    auto ret = std::pair<constellation::message::CSCP1Message::Type, std::string>();

    // Initialize requires frame
    ret = fsm.reactCSCP(TransitionCommand::initialize, {});
    REQUIRE(ret.first == CSCP1Message::Type::INCOMPLETE);
    REQUIRE_THAT(ret.second, Equals("Transition initialize requires a payload frame"));
    ret = fsm.reactCSCP(TransitionCommand::initialize, payload_frame);
    REQUIRE(ret.first == CSCP1Message::Type::SUCCESS);
    REQUIRE_THAT(ret.second, Equals("Transition initialize is being initiated"));

    // INVALID when not allowed
    satellite->progress_fsm(fsm);
    ret = fsm.reactCSCP(TransitionCommand::start, {});
    REQUIRE(ret.first == CSCP1Message::Type::INVALID);
    REQUIRE_THAT(ret.second, Equals("Transition start not allowed from INIT state"));

    // payload is ignored when not used
    ret = fsm.reactCSCP(TransitionCommand::launch, payload_frame);
    REQUIRE(ret.first == CSCP1Message::Type::SUCCESS);
    REQUIRE_THAT(ret.second, Equals("Transition launch is being initiated (payload frame is ignored)"));
    satellite->progress_fsm(fsm);

    // NOTIMPLEMENTED if reconfigure not supported
    satellite->dummy_support_reconfigure(false);
    ret = fsm.reactCSCP(TransitionCommand::reconfigure, std::move(payload_frame));
    REQUIRE(ret.first == CSCP1Message::Type::NOTIMPLEMENTED);
    REQUIRE_THAT(ret.second, Equals("Transition reconfigure is not implemented by this satellite"));
}

// NOLINTNEXTLINE(*-function-size)
TEST_CASE("Allowed FSM transitions", "[satellite][satellite::fsm]") {
    auto satellite = std::make_shared<DummySatellite>();
    auto fsm = FSM(satellite);
    using enum Transition;

    REQUIRE(fsm.getState() == State::NEW);
    // Allowed in NEW: initialize, failure
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from NEW state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from NEW state"));
    INFO("NEW succeeded");

    fsm.react(initialize);
    REQUIRE(fsm.getState() == State::initializing);
    // Allowed in initializing: initialized, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from initializing state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from initializing state"));
    INFO("initializing succeeded");

    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::INIT);
    // Allowed in INIT: initialize, launch, failure
    REQUIRE(fsm.isAllowed(initialize));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from INIT state"));
    REQUIRE(fsm.isAllowed(launch));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from INIT state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from INIT state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("INIT succeeded");

    fsm.react(launch);
    REQUIRE(fsm.getState() == State::launching);
    // Allowed in launching: launched, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from launching state"));
    REQUIRE(fsm.isAllowed(launched));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from launching state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from launching state"));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from launching state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("launching succeeded");

    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::ORBIT);
    // Allowed in ORBIT: start, land, reconfigure, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from ORBIT state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from ORBIT state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from ORBIT state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from ORBIT state"));
    REQUIRE(fsm.isAllowed(land));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from ORBIT state"));
    REQUIRE(fsm.isAllowed(reconfigure));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from ORBIT state"));
    REQUIRE(fsm.isAllowed(start));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from ORBIT state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from ORBIT state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from ORBIT state"));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from ORBIT state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("ORBIT succeeded");

    fsm.react(reconfigure);
    REQUIRE(fsm.getState() == State::reconfiguring);
    // Allowed in reconfiguring: reconfigured, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from reconfiguring state"));
    REQUIRE(fsm.isAllowed(reconfigured));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from reconfiguring state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from reconfiguring state"));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from reconfiguring state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("reconfiguring succeeded");

    satellite->progress_fsm(fsm);
    fsm.react(start);
    REQUIRE(fsm.getState() == State::starting);
    // Allowed in starting: started, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from starting state"));
    REQUIRE(fsm.isAllowed(started));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from starting state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from starting state"));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from starting state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("starting succeeded");

    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::RUN);
    // Allowed in RUN: stop, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from RUN state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from RUN state"));
    REQUIRE(fsm.isAllowed(stop));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from RUN state"));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from RUN state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("RUN succeeded");

    fsm.react(stop);
    REQUIRE(fsm.getState() == State::stopping);
    // Allowed in stopping: stopped, interrupt, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from stopping state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from stopping state"));
    REQUIRE(fsm.isAllowed(stopped));
    REQUIRE(fsm.isAllowed(interrupt));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from stopping state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("stopping succeeded");

    satellite->progress_fsm(fsm);
    fsm.react(land);
    REQUIRE(fsm.getState() == State::landing);
    // Allowed in landing: landed, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from landing state"));
    REQUIRE(fsm.isAllowed(landed));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from landing state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from landing state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("landing succeeded");

    satellite->progress_fsm(fsm);
    fsm.react(launch);
    fsm.react(interrupt);
    REQUIRE(fsm.getState() == State::interrupting);
    // Allowed in interrupting: interrupted, failure
    REQUIRE_THROWS_WITH(fsm.react(initialize), Equals("Transition initialize not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from interrupting state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from interrupting state"));
    REQUIRE(fsm.isAllowed(interrupted));
    REQUIRE(fsm.isAllowed(failure));
    INFO("interrupting succeeded");

    satellite->progress_fsm(fsm);
    REQUIRE(fsm.getState() == State::SAFE);
    // Allowed in SAFE: initialize, failure
    REQUIRE(fsm.isAllowed(initialize));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from SAFE state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from SAFE state"));
    REQUIRE(fsm.isAllowed(failure));
    INFO("SAFE succeeded");

    fsm.react(failure);
    REQUIRE(fsm.getState() == State::ERROR);
    // Allowed in ERROR: initialize
    REQUIRE(fsm.isAllowed(initialize));
    REQUIRE_THROWS_WITH(fsm.react(initialized), Equals("Transition initialized not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(launch), Equals("Transition launch not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(launched), Equals("Transition launched not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(land), Equals("Transition land not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(landed), Equals("Transition landed not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigure), Equals("Transition reconfigure not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(reconfigured), Equals("Transition reconfigured not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(start), Equals("Transition start not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(started), Equals("Transition started not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(stop), Equals("Transition stop not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(stopped), Equals("Transition stopped not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupt), Equals("Transition interrupt not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(interrupted), Equals("Transition interrupted not allowed from ERROR state"));
    REQUIRE_THROWS_WITH(fsm.react(failure), Equals("Transition failure not allowed from ERROR state"));
    INFO("ERROR succeeded");
}

// NOLINTEND(cert-err58-cpp,misc-use-anonymous-namespace)
