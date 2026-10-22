#pragma once

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <string_view>

namespace dv {

    // inline constexpr int MAX_12_BIT = 0xFFFF; // COMPAT: Original DoViBaker bit-perfect fallback
    inline constexpr int MAX_12_BIT = 0xFFF;  // 4095
    inline constexpr int MAX_16_BIT = 0xFFFF; // 65535

    struct Channel final {
        static constexpr int Y = 0, U = 1, V = 2;
        static constexpr int R = 0, G = 1, B = 2;
    };

    constexpr int stride(ptrdiff_t stride) noexcept {
        return static_cast<int>(stride / sizeof(uint16_t));
    }

    template<std::integral T>
    constexpr int64_t fixedPoint(T intPart, uint64_t fracPart, int fracBits) noexcept {
        return (static_cast<int64_t>(intPart) << fracBits) + static_cast<int64_t>(fracPart);
    }

    constexpr void expect(bool condition, std::string_view msg) {
        if (condition) [[likely]] return;
        throw std::runtime_error(msg.data());
    }

    template<bool... Flags>
    constexpr void dispatch(auto&& handler) {
        handler(std::integral_constant<bool, Flags>{}...);
    }

    template<bool... Flags>
    constexpr void dispatch(auto&& handler, bool head, auto... tail) {
        if (head) dispatch<Flags..., true>(handler, tail...);
        else dispatch<Flags..., false>(handler, tail...);
    }

    namespace pq {
        inline constexpr double ST2084_L_MAX  = 10000.0;
        inline constexpr double ST2084_C1     = (3424.0 / 4096.0);
        inline constexpr double ST2084_C2     = (2413.0 / 4096.0) * 32.0;
        inline constexpr double ST2084_C3     = (2392.0 / 4096.0) * 32.0;
        inline constexpr double ST2084_M1     = (2610.0 / 4096.0) / 4.0;
        inline constexpr double ST2084_M2     = (2523.0 / 4096.0) * 128.0;
        inline constexpr double ST2084_M1_INV = 1.0 / ST2084_M1;
        inline constexpr double ST2084_M2_INV = 1.0 / ST2084_M2;
        inline constexpr double PQ_MAX_12_BIT = 4095.0;

        inline double toNits(double ep) noexcept {
            const double exp = std::pow(ep, ST2084_M2_INV);
            const double num = std::max(exp - ST2084_C1, 0.0);
            const double den = ST2084_C2 - ST2084_C3 * exp;
            const double lin = std::pow(num / den, ST2084_M1_INV);
            return lin * ST2084_L_MAX;
        }

        inline double toPq(double nits) noexcept {
            const double fd  = std::clamp(nits / ST2084_L_MAX, 0.0, 1.0);
            const double exp = std::pow(fd, ST2084_M1);
            const double num = ST2084_C1 + ST2084_C2 * exp;
            const double den = 1.0 + ST2084_C3 * exp;
            return std::pow(num / den, ST2084_M2);
        }
    }

    inline double pq12ToNits(uint16_t pq) noexcept {
        const double ep = pq / pq::PQ_MAX_12_BIT;
        return pq::toNits(ep);
    }

    inline uint16_t nitsToPq12(double nits) noexcept {
        return std::lround(pq::toPq(nits) * pq::PQ_MAX_12_BIT);
    }

}
