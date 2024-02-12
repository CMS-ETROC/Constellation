/**
 * @file
 * @brief Example implementation of CHIRP manager
 *
 * @copyright Copyright (c) 2023 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#include <any>
#include <charconv>
#include <iomanip>
#include <iostream>
#include <optional>
#include <ranges>
#include <string>
#include <string_view>
#include <vector>

#include <asio.hpp>
#include <magic_enum.hpp>

#include "constellation/chirp/Manager.hpp"
#include "constellation/chirp/Message.hpp"
#include "constellation/chirp/protocol_info.hpp"

using namespace constellation::chirp;
using namespace std::literals::string_literals;
using namespace std::literals::string_view_literals;

enum class Command {
    list_registered_services,
    list_discovered_services,
    register_service,
    unregister_service,
    register_callback,
    unregister_callback,
    request,
    reset,
    quit,
};
using enum Command;

template <typename T> std::string pad_str_right(T&& string, std::size_t width) {
    std::string out {string.data(), string.size()};
    while(out.size() < width) {
        out += ' ';
    }
    return out;
}

// DiscoverCallback signature NOLINTNEXTLINE(performance-unnecessary-value-param)
void discover_callback(DiscoveredService service, bool depart, std::any /* user_data */) {
    std::cout << "Callback:\n"
              << " Service " << pad_str_right(magic_enum::enum_name(service.identifier), 10) //
              << " Port " << std::setw(5) << service.port                                    //
              << " Host " << service.host_id.to_string()                                     //
              << " IP " << pad_str_right(service.address.to_string(), 15)                    //
              << (depart ? " DEPART" : " OFFER")                                             //
              << std::endl;
}

int main(int argc, char* argv[]) {
    // Specify satellite name, brd address, group name and any address via cmdline
    auto name = "control"s;
    asio::ip::address brd_address = asio::ip::address_v4::broadcast();
    auto group = "cnstln1"s;
    asio::ip::address any_address = asio::ip::address_v4::any();
    if(argc >= 2) {
        name = argv[1];
    }
    if(argc >= 3) {
        try {
            brd_address = asio::ip::make_address(argv[2]);
        } catch(const asio::system_error& error) {
            std::cerr << "Unable to use broadcast address " << std::quoted(argv[2]) << ", using "
                      << std::quoted(brd_address.to_string()) << " instead" << std::endl;
        }
    }
    if(argc >= 4) {
        group = argv[3];
    }
    if(argc >= 5) {
        try {
            any_address = asio::ip::make_address(argv[4]);
        } catch(const asio::system_error& error) {
            std::cerr << "Unable to use any address " << std::quoted(argv[4]) << ", using "
                      << std::quoted(any_address.to_string()) << " instead" << std::endl;
        }
    }

    Manager manager {brd_address, any_address, group, name};

    std::cout << "Commands: "
              << "\n list_registered_services"
              << "\n list_discovered_services <ServiceIdentifier>"
              << "\n register_service <ServiceIdentifier:CONTROL> <Port:23999>"
              << "\n unregister_service <ServiceIdentifier:CONTROL> <Port:23999>"
              << "\n register_callback <ServiceIdentifier:CONTROL>"
              << "\n unregister_callback <ServiceIdentifier:CONTROL>"
              << "\n request <ServiceIdentifier:CONTROL>"
              << "\n reset" << std::endl;

    try {
        manager.Start();

        while(true) {
            std::string cmd_input {};
            std::getline(std::cin, cmd_input);

            // Split command by spaces to vector of string views
            std::vector<std::string_view> cmd_split {};
            for(const auto word_range : std::ranges::views::split(cmd_input, " "sv)) {
                cmd_split.emplace_back(&*word_range.begin(), std::ranges::distance(word_range));
            }

            // If not a command, continue
            if(cmd_split.empty()) {
                continue;
            }
            auto cmd_opt = magic_enum::enum_cast<Command>(cmd_split[0]);
            if(!cmd_opt.has_value()) {
                std::cout << std::quoted(cmd_split[0]) << " is not a valid command" << std::endl;
                continue;
            }
            auto cmd = cmd_opt.value();

            // List registered services
            if(cmd == list_registered_services) {
                auto registered_services = manager.GetRegisteredServices();
                std::cout << " Registered Services:\n";
                for(const auto& service : registered_services) {
                    std::cout << " Service " << pad_str_right(magic_enum::enum_name(service.identifier), 10) //
                              << " Port " << std::setw(5) << service.port                                    //
                              << "\n";
                }
                std::cout << std::flush;
                continue;
            }
            // List discovered services
            if(cmd == list_discovered_services) {
                std::optional<ServiceIdentifier> service_opt {std::nullopt};
                if(cmd_split.size() >= 2) {
                    service_opt = magic_enum::enum_cast<ServiceIdentifier>(cmd_split[1]);
                }
                auto discovered_services = service_opt.has_value() ? manager.GetDiscoveredServices(service_opt.value())
                                                                   : manager.GetDiscoveredServices();
                std::cout << " Discovered Services:\n";
                for(const auto& service : discovered_services) {
                    std::cout << " Service " << pad_str_right(magic_enum::enum_name(service.identifier), 15) //
                              << " Port " << std::setw(5) << service.port                                    //
                              << " Host " << service.host_id.to_string()                                     //
                              << " IP " << pad_str_right(service.address.to_string(), 15)                    //
                              << "\n";
                }
                std::cout << std::flush;
                continue;
            }
            // Register or unregister a service
            if(cmd == register_service || cmd == unregister_service) {
                ServiceIdentifier service {CONTROL};
                if(cmd_split.size() >= 2) {
                    service = magic_enum::enum_cast<ServiceIdentifier>(cmd_split[1]).value_or(CONTROL);
                }
                Port port {23999};
                if(cmd_split.size() >= 3) {
                    std::from_chars(cmd_split[2].data(), cmd_split[2].data() + cmd_split[2].size(), port);
                }
                if(cmd == register_service) {
                    auto ret = manager.RegisterService(service, port);
                    if(ret) {
                        std::cout << " Registered Service " << pad_str_right(magic_enum::enum_name(service), 10) //
                                  << " Port " << std::setw(5) << port << std::endl;
                    }
                } else {
                    auto ret = manager.UnregisterService(service, port);
                    if(ret) {
                        std::cout << " Unregistered Service " << pad_str_right(magic_enum::enum_name(service), 10) //
                                  << " Port " << std::setw(5) << port << std::endl;
                    }
                }
                continue;
            }
            // Register of unregister callback
            if(cmd == register_callback || cmd == unregister_callback) {
                ServiceIdentifier service {CONTROL};
                if(cmd_split.size() >= 2) {
                    service = magic_enum::enum_cast<ServiceIdentifier>(cmd_split[1]).value_or(CONTROL);
                }
                if(cmd == register_callback) {
                    auto ret = manager.RegisterDiscoverCallback(&discover_callback, service, nullptr);
                    if(ret) {
                        std::cout << " Registered Callback for " << magic_enum::enum_name(service) << std::endl;
                    }
                } else {
                    auto ret = manager.UnregisterDiscoverCallback(&discover_callback, service);
                    if(ret) {
                        std::cout << " Unregistered Callback for " << magic_enum::enum_name(service) << std::endl;
                    }
                }
                continue;
            }
            // Send CHIRP request
            if(cmd == request) {
                ServiceIdentifier service {CONTROL};
                if(cmd_split.size() >= 2) {
                    service = magic_enum::enum_cast<ServiceIdentifier>(cmd_split[1]).value_or(CONTROL);
                }
                manager.SendRequest(service);
                std::cout << " Sent Request for " << magic_enum::enum_name(service) << std::endl;
                continue;
            }
            // Reset
            if(cmd == reset) {
                manager.UnregisterDiscoverCallbacks();
                manager.UnregisterServices();
                manager.ForgetDiscoveredServices();
                continue;
            }
            // Quit
            if(cmd == quit) {
                break;
            }
        }
    } catch(...) {
    }
}
