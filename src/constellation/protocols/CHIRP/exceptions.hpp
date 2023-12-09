/**
 * @file
 * @brief CHIRP protocol exceptions
 *
 * @copyright Copyright (c) 2023 DESY and the Constellation authors.
 * This software is distributed under the terms of the EUPL-1.2 License, copied verbatim in the file "LICENSE.md".
 * SPDX-License-Identifier: EUPL-1.2
 */

#pragma once

#include <exception>
#include <string>

namespace cnstln::CHIRP {

/** Error thrown when a CHIRP message was not decoded successfully */
class DecodeError : public std::exception {
public:
    /**
     * @param error_message Error message
     */
    DecodeError(std::string error_message) : error_message_(std::move(error_message)) {}

    /**
     * @returns Error message
     */
    const char* what() const noexcept final { return error_message_.c_str(); }

protected:
    std::string error_message_;
};

} // namespace cnstln::CHIRP
