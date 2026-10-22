#include "Spline16Resampler.h"

#include <algorithm>
#include <array>
#include <memory>

#include "VapourSynth4.h"

#include "Utils.h"
#include "VSPtr.h"

namespace {

    enum class PlaneType { LUMA, CHROMA };

    template<PlaneType Type>
    struct UpsampleTraits;

    /*
    * these upsampling functions are not following the paper, but should be correct when assuming top-left chroma location
    */
    template<>
    struct UpsampleTraits<PlaneType::CHROMA> final {
        static constexpr std::array offsets{-1, 0, 1, 2};
        static constexpr int len = std::size(offsets);
        static constexpr int n0  = 1; // zero index
        static constexpr int pD  = 2; // right padding

        static constexpr uint16_t upscaleEven(const uint16_t* samples) noexcept {
            return samples[n0];
        }

        static constexpr uint16_t upscaleOdd(const uint16_t* samples) noexcept {
            // this is spline16 at positions 0.5, 1.5
            const int val = (-307 * samples[n0 - 1] + 2355 * samples[n0] + 2355 * samples[n0 + 1] - 307 * samples[n0 + 2] + 2048) >> 12;
            return std::clamp(val, 0, dv::MAX_12_BIT);
        }
    };

    template<>
    struct UpsampleTraits<PlaneType::LUMA> final {
        static constexpr std::array offsets{-2, -1, 0, 1, 2};
        static constexpr int len = std::size(offsets);
        static constexpr int n0  = 2; // zero index
        static constexpr int pD  = 2; // right padding

        static constexpr uint16_t upscaleEven(const uint16_t* samples) noexcept {
            // this matches quite exactly spline16 at positions -1.75, -0.75, 0.25, 1.25
            const int val = (-3 * samples[n0 - 2] + 29 * samples[n0 - 1] + 111 * samples[n0] - 9 * samples[n0 + 1] + 64) >> 7;
            return std::clamp(val, 0, dv::MAX_12_BIT);
        }

        static constexpr uint16_t upscaleOdd(const uint16_t* samples) noexcept {
            // this matches quite exactly spline16 at positions -1.25, -0.25, 0.75, 1.75
            const int val = (-9 * samples[n0 - 1] + 111 * samples[n0] + 29 * samples[n0 + 1] - 3 * samples[n0 + 2] + 64) >> 7;
            return std::clamp(val, 0, dv::MAX_12_BIT);
        }
    };

    template<PlaneType Type, typename T = UpsampleTraits<Type>>
    void upsampleVert(const VSFrame* src, VSFrame* dst, int plane, const VSAPI* vsapi) noexcept {
        const int srcHeight = vsapi->getFrameHeight(src, plane);
        const int srcWidth  = vsapi->getFrameWidth(src, plane);
        const int srcPitch  = dv::stride(vsapi->getStride(src, plane));
        const int dstPitch  = dv::stride(vsapi->getStride(dst, plane));

        const auto* __restrict srcP = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(src, plane));
        auto* __restrict dstEven    = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, plane));
        auto* __restrict dstOdd     = dstEven + dstPitch;

        std::array<const uint16_t*, T::len> srcRow{};
        std::array<uint16_t, T::len> samples{};

        for (int y0 = 0; y0 < srcHeight; ++y0) [[likely]] {
            for (int n = 0; n < T::len; ++n) {
                const int y = std::clamp(y0 + T::offsets[n], 0, srcHeight - 1);
                srcRow[n]   = srcP + y * srcPitch;
            }

            for (int x = 0; x < srcWidth; ++x) [[likely]] {
                for (int n = 0; n < T::len; ++n) {
                    samples[n] = srcRow[n][x];
                }

                dstEven[x] = T::upscaleEven(samples.data());
                dstOdd[x]  = T::upscaleOdd(samples.data());
            }

            dstEven += 2 * dstPitch;
            dstOdd  += 2 * dstPitch;
        }
    }

    template<PlaneType Type, typename T = UpsampleTraits<Type>>
    void upsampleHorz(const VSFrame* src, VSFrame* dst, int plane, const VSAPI* vsapi) noexcept {
        const int srcHeight = vsapi->getFrameHeight(src, plane);
        const int srcWidth  = vsapi->getFrameWidth(src, plane);
        const int srcStride = dv::stride(vsapi->getStride(src, plane));
        const int dstStride = dv::stride(vsapi->getStride(dst, plane));

        const auto* __restrict srcP = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(src, plane));
        auto* __restrict dstP       = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, plane));

        const auto processEdge = [&](int x0) noexcept {
            std::array<uint16_t, T::len> samples{};
            for (int n = 0; n < T::len; ++n) {
                samples[n] = srcP[std::clamp(x0 + T::offsets[n], 0, srcWidth - 1)];
            }

            dstP[2 * x0]     = T::upscaleEven(samples.data());
            dstP[2 * x0 + 1] = T::upscaleOdd(samples.data());
        };

        for (int y = 0; y < srcHeight; ++y) [[likely]] {
            for (int x0 = 0; x0 < T::n0; ++x0) {
                processEdge(x0);
            }

            for (int x0 = T::n0; x0 < srcWidth - T::pD; ++x0) [[likely]] {
                dstP[2 * x0]     = T::upscaleEven(&srcP[x0 - T::n0]);
                dstP[2 * x0 + 1] = T::upscaleOdd(&srcP[x0 - T::n0]);
            }

            for (int x0 = srcWidth - T::pD; x0 < srcWidth; ++x0) {
                processEdge(x0);
            }

            srcP += srcStride;
            dstP += dstStride;
        }
    }

    template<bool IncludeLuma>
    auto upsample(const VSFrame* src, const VSVideoFormat* dstFmt, int width, int height, VSCore* core, const VSAPI* vsapi) noexcept {
        const auto mez = dv::makeFrame(vsapi, dstFmt, width / 2, height, core);
        if constexpr (IncludeLuma) upsampleVert<PlaneType::LUMA>(src, mez.get(), dv::Channel::Y, vsapi);
        upsampleVert<PlaneType::CHROMA>(src, mez.get(), dv::Channel::U, vsapi);
        upsampleVert<PlaneType::CHROMA>(src, mez.get(), dv::Channel::V, vsapi);

        auto dst = dv::makeFrame(vsapi, dstFmt, width, height, core);
        if constexpr (IncludeLuma) upsampleHorz<PlaneType::LUMA>(mez.get(), dst.get(), dv::Channel::Y, vsapi);
        upsampleHorz<PlaneType::CHROMA>(mez.get(), dst.get(), dv::Channel::U, vsapi);
        upsampleHorz<PlaneType::CHROMA>(mez.get(), dst.get(), dv::Channel::V, vsapi);

        return dst;
    }

}

namespace dv {

    std::unique_ptr<VSFrame, VSDeleter> resample(const VSFrame* src, const VSVideoFormat* dstFmt, int width, int height, VSCore* core, const VSAPI* vsapi) noexcept {
        return upsample<true>(src, dstFmt, width, height, core, vsapi);
    }

    std::unique_ptr<VSFrame, VSDeleter> resampleChroma(const VSFrame* src, const VSVideoFormat* dstFmt, int width, int height, VSCore* core, const VSAPI* vsapi) noexcept {
        return upsample<false>(src, dstFmt, width, height, core, vsapi);
    }

}
