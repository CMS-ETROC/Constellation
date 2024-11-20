/**
 * @file
 * @brief Helpers for ZeroMQ
 *
 * @copyright Copyright (c) 2024 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include <charconv>
#include <cstdint>
#include <memory>
#include <set>
#include <string_view>

#include <asio/ip/address_v4.hpp>
#ifndef _WIN32
#include <ifaddrs.h>
#include <net/if.h> // NOLINT(misc-include-cleaner)
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#endif
#include <zmq.hpp>

#include "constellation/build.hpp"

namespace constellation::utils {

    /**
     * @brief Port number for a network connection
     *
     * Note that most ports in Constellation are ephemeral ports, meaning that the port numbers are allocated dynamically.
     * See also https://en.wikipedia.org/wiki/Ephemeral_port.
     */
    using Port = std::uint16_t;

    /**
     * @brief Bind ZeroMQ socket to wildcard address with ephemeral port
     *
     * See also https://libzmq.readthedocs.io/en/latest/zmq_tcp.html.
     *
     * @param socket Reference to socket which should be bound
     * @return Ephemeral port assigned by operating system
     */
    CNSTLN_API inline Port bind_ephemeral_port(zmq::socket_t& socket) {
        Port port {};

        // Bind to wildcard address and port to let operating system assign an ephemeral port
        socket.bind("tcp://*:*");

        // Get address with ephemeral port via last endpoint
        const auto endpoint = socket.get(zmq::sockopt::last_endpoint);

        // Note: endpoint is always "tcp://0.0.0.0:XXXXX", thus port number starts at character 14
        const auto port_substr = std::string_view(endpoint).substr(14);
        std::from_chars(port_substr.cbegin(), port_substr.cend(), port);

        return port;
    }

    /**
     * @brief Return the global ZeroMQ context
     *
     * @note Since the global ZeroMQ context is static, static classes need to store an instance of the shared pointer.
     *
     * @return Shared pointer to the global ZeroMQ context
     */
    CNSTLN_API inline std::shared_ptr<zmq::context_t>& global_zmq_context() {
        static auto context = std::make_shared<zmq::context_t>();
        // Switch off blocky behavior of context - corresponds to setting linger = 0 for all sockets
        context->set(zmq::ctxopt::blocky, 0);
        return context;
    }

    CNSTLN_API inline std::set<asio::ip::address_v4> get_broadcast_addresses() {
        std::set<asio::ip::address_v4> addresses;

#ifdef _WIN32
        // On MinGW use the default broadcast address of the system
        asio::ip::address_v4 default_brd_addr;
        try {
            default_brd_addr = asio::ip::address_v4::broadcast();
        } catch(const asio::system_error& error) {
            default_brd_addr = asio::ip::make_address_v4("255.255.255.255");
        }
        addresses.emplace(default_brd_addr);
#else
        // Obtain linked list of all local network interfaces
        struct ifaddrs* addrs = nullptr;
        struct ifaddrs* ifa = nullptr;
        if(getifaddrs(&addrs) != 0) {
            return {};
        }

        // Iterate through list of interfaces
        for(ifa = addrs; ifa != nullptr; ifa = ifa->ifa_next) {

            // Select only running interfaces and those providing IPV4:
            if(ifa->ifa_addr == nullptr || ((ifa->ifa_flags & IFF_RUNNING) == 0U) || ifa->ifa_addr->sa_family != AF_INET) {
                continue;
            }

            // Ensure that the interface holds a broadcast address
            if((ifa->ifa_flags & IFF_BROADCAST) == 0U) {
                continue;
            }

/**
 * The Linux kernel provides a union called "ifa_ifu" which contains either the ifu_broadaddr or the ifu_dstaddr, depending
 * on whether or not the IFF_BROADCAST flag is set.
 *
 * The Apple kernel provides a field called "ifa_dstaddr" as well as an alias "ifa_broadaddr" pointing to the same memory.
 * The memory holds the broadcast address if IFF_BROADCAST is set.
 */
#ifndef ifa_broadaddr
#define ifa_broadaddr ifa_ifu.ifu_broadaddr
#endif

            char buffer[NI_MAXHOST];           // NOLINT(modernize-avoid-c-arrays)
            if(getnameinfo(ifa->ifa_broadaddr, // NOLINT(cppcoreguidelines-pro-type-union-access)
                           sizeof(struct sockaddr_in),
                           buffer, // NOLINT(cppcoreguidelines-pro-bounds-array-to-pointer-decay)
                           sizeof(buffer),
                           nullptr,
                           0,
                           NI_NUMERICHOST) == 0) {

                try {
                    // NOLINTNEXTLINE(cppcoreguidelines-pro-bounds-array-to-pointer-decay)
                    addresses.emplace(asio::ip::make_address_v4(buffer));
                } catch(const asio::system_error& error) {
                    continue;
                }
            }
        }

        freeifaddrs(addrs);
#endif
        return addresses;
    }

} // namespace constellation::utils
