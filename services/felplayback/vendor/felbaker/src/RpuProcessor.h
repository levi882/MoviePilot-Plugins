#pragma once

#include <algorithm>
#include <array>

#include "rpu_parser.h"

#include "Utils.h"

class alignas(64) RpuProcessor final {
public:
    struct YccToRgb final {
        std::array<int, 3> offsets{};
        std::array<int16_t, 9> coeffs{};
    };

    struct Metadata final {
        bool sceneRefresh{};
        bool fullRange{};
        uint16_t dynamicMinPq{};
        uint16_t dynamicMaxPq{};
        uint16_t dynamicMaxCLL{};
        uint16_t staticMaxMDL{};
        uint16_t staticMinMDL{};
        uint16_t staticMaxCLL{};
        uint16_t staticMaxFALL{};
        uint16_t staticMaxPq{};
    };

    RpuProcessor(const uint8_t* rpuBuf, int rpuSize, int blContainerBits, int elContainerBits, bool doviMetadata);

    [[nodiscard]] const YccToRgb& getYccToRgb() const noexcept { return yccToRgb; }
    [[nodiscard]] const Metadata& getMetadata() const noexcept { return metadata; }
    [[nodiscard]] bool useEl() const noexcept { return !skipEl; }
    [[nodiscard]] bool isPolynomialY() const noexcept { return isPolynomial[dv::Channel::Y]; }
    [[nodiscard]] bool isPolynomialU() const noexcept { return isPolynomial[dv::Channel::U]; }
    [[nodiscard]] bool isPolynomialV() const noexcept { return isPolynomial[dv::Channel::V]; }

    template<bool ApplyEl>
    [[nodiscard]] uint16_t mapY(uint16_t bl, uint16_t el) const noexcept {
        return map<dv::Channel::Y, true, ApplyEl>(bl, el, 0, 0, 0);
    }

    template<bool Polynomial, bool ApplyEl>
    [[nodiscard]] uint16_t mapU(uint16_t bl, uint16_t el, uint16_t mmrBlY, uint16_t mmrBlU, uint16_t mmrBlV) const noexcept {
        return map<dv::Channel::U, Polynomial, ApplyEl>(bl, el, mmrBlY, mmrBlU, mmrBlV);
    }

    template<bool Polynomial, bool ApplyEl>
    [[nodiscard]] uint16_t mapV(uint16_t bl, uint16_t el, uint16_t mmrBlY, uint16_t mmrBlU, uint16_t mmrBlV) const noexcept {
        return map<dv::Channel::V, Polynomial, ApplyEl>(bl, el, mmrBlY, mmrBlU, mmrBlV);
    }

    static void toRGB(const YccToRgb& yccToRgb, uint16_t y, uint16_t u, uint16_t v, uint16_t& r, uint16_t& g, uint16_t& b) noexcept {
        const int yf = (y << YCC_TO_RGB_INPUT_SHIFT) - yccToRgb.offsets[dv::Channel::Y];
        const int uf = (u << YCC_TO_RGB_INPUT_SHIFT) - yccToRgb.offsets[dv::Channel::U];
        const int vf = (v << YCC_TO_RGB_INPUT_SHIFT) - yccToRgb.offsets[dv::Channel::V];

        // // COMPAT: Original DoViBaker bit-perfect fallback
        // const int yf = y - yccToRgb.offsets[dv::Channel::Y];
        // const int uf = u - yccToRgb.offsets[dv::Channel::U];
        // const int vf = v - yccToRgb.offsets[dv::Channel::V];

        r = std::clamp((yccToRgb.coeffs[0] * yf + yccToRgb.coeffs[1] * uf + yccToRgb.coeffs[2] * vf) >> YCC_TO_RGB_COEFF_SHIFT, 0, dv::MAX_16_BIT);
        g = std::clamp((yccToRgb.coeffs[3] * yf + yccToRgb.coeffs[4] * uf + yccToRgb.coeffs[5] * vf) >> YCC_TO_RGB_COEFF_SHIFT, 0, dv::MAX_16_BIT);
        b = std::clamp((yccToRgb.coeffs[6] * yf + yccToRgb.coeffs[7] * uf + yccToRgb.coeffs[8] * vf) >> YCC_TO_RGB_COEFF_SHIFT, 0, dv::MAX_16_BIT);
    }

private:
    static constexpr int DV_LUTS                 = 4096;
    static constexpr int DV_MAX_SEGMENTS         = 8;
    static constexpr int DV_COMPONENTS           = 3;
    // static constexpr int MAP_OUT_BIT_DEPTH       = 16; // COMPAT: Original DoViBaker bit-perfect fallback
    static constexpr int MAP_OUT_BIT_DEPTH       = 12;
    static constexpr int RGB_OUT_BIT_DEPTH       = 16;
    static constexpr int YCC_TO_RGB_COEFF_SHIFT  = 13;
    static constexpr int YCC_TO_RGB_OFFSET_SHIFT = 28 - RGB_OUT_BIT_DEPTH;
    static constexpr int YCC_TO_RGB_INPUT_SHIFT  = RGB_OUT_BIT_DEPTH - MAP_OUT_BIT_DEPTH;

    struct Params final {
        int blBitDepth{};
        int blShift{};
        int elShift{};
        int mmrShift{};
        int mappingShift{};
        int reconOffset{};
        int reconShift{};
        int reconLimit{};
        int mapOutShift{}; // Normalizes map output to 12-bit
    };

    struct PivotBounds final {
        int min{};
        int max{};
    };

    struct alignas(64) Segment final {
        std::array<int64_t, 21> coeffs{};
        int64_t mmrConstant{};
        int order{};
    };

    Params params{};
    std::array<PivotBounds, 3> pivots{};

    alignas(64) YccToRgb yccToRgb{};

    Metadata metadata{};
    bool skipEl{};
    std::array<bool, DV_COMPONENTS> isPolynomial{};

    alignas(64) std::array<std::array<Segment, DV_MAX_SEGMENTS>, DV_COMPONENTS> segments{};

    alignas(64) std::array<std::array<uint8_t, DV_LUTS>, DV_COMPONENTS> pivotLut{};
    alignas(64) std::array<std::array<int16_t, DV_LUTS>, DV_COMPONENTS> nlqLut{};

    template<int Cmp, bool Polynomial, bool ApplyEl>
    [[nodiscard]] uint16_t map(uint16_t bl, uint16_t el, uint16_t mmrBlY, uint16_t mmrBlU, uint16_t mmrBlV) const noexcept {
        const int blShift = params.blShift;
        const int pivot   = pivotLut[Cmp][bl >> blShift];

        int result = Polynomial
            ? mapPolynomial<Cmp>(bl >> blShift, pivot)
            : mapMmr<Cmp>(mmrBlY >> blShift, mmrBlU >> blShift, mmrBlV >> blShift, pivot);

        if constexpr (ApplyEl) {
            result += nlqLut[Cmp][el >> params.elShift];
        }

        //signal reconstruction
        result = (result + params.reconOffset) >> params.reconShift;
        return std::clamp(result, 0, params.reconLimit) << params.mapOutShift;
    }

    template<int Cmp>
    [[nodiscard]] uint16_t mapPolynomial(int sample, int pivot) const noexcept {
        const int depth = params.blBitDepth;
        const int64_t x = std::clamp(sample, pivots[Cmp].min, pivots[Cmp].max);

        const int64_t* __restrict c = segments[Cmp][pivot].coeffs.data();

        // compute polynom at s in fixed point arithmetic
        int64_t result;
        switch (segments[Cmp][pivot].order) {
            [[likely]] case 1:
                result = ((c[1]   * x) >> depth) + c[0];
                break;
            [[unlikely]] case 2:
                result = ((c[2]   * x) >> depth) + c[1];
                result = ((result * x) >> depth) + c[0];
                break;
            [[unlikely]] case 3:
                result = ((c[3]   * x) >> depth) + c[2];
                result = ((result * x) >> depth) + c[1];
                result = ((result * x) >> depth) + c[0];
                break;
            [[unlikely]] default:
                result = c[0];
                break;
        }

        result = std::max<int64_t>(0, result) >> params.mappingShift;
        return std::min<int64_t>(result, dv::MAX_16_BIT);
    }

    template<int Cmp>
    [[nodiscard]] uint16_t mapMmr(int sampleY, int sampleU, int sampleV, int pivot) const noexcept {
        const int64_t x = std::clamp(sampleY, pivots[dv::Channel::Y].min, pivots[dv::Channel::Y].max) << params.mmrShift;
        const int64_t y = std::clamp(sampleU, pivots[dv::Channel::U].min, pivots[dv::Channel::U].max) << params.mmrShift;
        const int64_t z = std::clamp(sampleV, pivots[dv::Channel::V].min, pivots[dv::Channel::V].max) << params.mmrShift;

        const int64_t* __restrict c = segments[Cmp][pivot].coeffs.data();

        int64_t result = segments[Cmp][pivot].mmrConstant;
        if (const int order = segments[Cmp][pivot].order; order >= 1) [[likely]] {
            const int64_t xy  = (x * y)  >> 20;
            const int64_t xz  = (x * z)  >> 20;
            const int64_t yz  = (y * z)  >> 20;
            const int64_t xyz = (xy * z) >> 20;

            result += c[0] * x +  c[1] * y  + c[2] * z  +
                      c[3] * xy + c[4] * xz + c[5] * yz + c[6] * xyz;

            if (order >= 2) [[likely]] {
                const int64_t x2   = (x * x)     >> 20;
                const int64_t y2   = (y * y)     >> 20;
                const int64_t z2   = (z * z)     >> 20;
                const int64_t xy2  = (xy * xy)   >> 20;
                const int64_t xz2  = (xz * xz)   >> 20;
                const int64_t yz2  = (yz * yz)   >> 20;
                const int64_t xyz2 = (xyz * xyz) >> 20;

                result += c[7]  * x2  + c[8]  * y2  + c[9]  * z2  +
                          c[10] * xy2 + c[11] * xz2 + c[12] * yz2 + c[13] * xyz2;

                if (order >= 3) [[likely]] {
                    result += c[14] * ((x * x2)   >> 20) + c[15] * ((y * y2)   >> 20) + c[16] * ((z * z2)   >> 20) +
                              c[17] * ((xy * xy2) >> 20) + c[18] * ((xz * xz2) >> 20) + c[19] * ((yz * yz2) >> 20) + c[20] * ((xyz * xyz2) >> 20);
                }
            }
        }

        result = std::max<int64_t>(0, result) >> params.mappingShift;
        return std::min<int64_t>(result, dv::MAX_16_BIT);
    }

    inline void initMappingParams(const DoviRpuDataMapping& mappingData, int coeffLog2Denom);
    inline void initPivots(const DoviU16Data& pivotDeltas, int cmp) noexcept;
    inline void initPolynomialParams(const DoviPolynomialCurve& polynomial, int cmp, int segmentCount, int coeffLog2Denom) noexcept;
    inline void initMmrParams(const DoviMMRCurve& mmr, int cmp, int segmentCount, int coeffLog2Denom) noexcept;
    inline void initNlqLut(const DoviRpuDataNlq& nlq, int elBitDepth, int coeffLog2Denom) noexcept;
    inline void initRgbParams(const DoviVdrDmData& vdrDm) noexcept;
    inline void initMetadata(const DoviVdrDmData& vdrDm, bool doviMetadata) noexcept;
};
