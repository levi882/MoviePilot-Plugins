#include "FelBaker.h"

#include <memory>
#include <stdexcept>

#include "VapourSynth4.h"

#include "RpuProcessor.h"
#include "Spline16Resampler.h"
#include "Utils.h"
#include "VSPtr.h"

namespace {

    void rgbProperties(VSMap* props, const VSAPI* vsapi) noexcept {
        vsapi->mapDeleteKey(props, "_ChromaLocation");        //RGB has no chroma location defined
        vsapi->mapSetInt(props, "_Matrix", 0, maReplace);     //output is RGB
        vsapi->mapSetInt(props, "_ColorRange", 0, maReplace); // 0: full, 1: limited
    }

    template<bool YUV>
    void setFrameProperties(VSFrame* dst, const RpuProcessor& rpuProc, bool doviMetadata, const VSAPI* vsapi) noexcept {
        const auto& metadata = rpuProc.getMetadata();
        VSMap* props = vsapi->getFramePropertiesRW(dst);

        if constexpr (YUV) {
            vsapi->mapSetData(props, "_FelBaker_YccToRGB", reinterpret_cast<const char*>(&rpuProc.getYccToRgb()), sizeof(rpuProc.getYccToRgb()), dtBinary, maReplace);
            vsapi->mapSetInt(props, "_ColorRange", !metadata.fullRange, maReplace); // 0: full, 1: limited
        } else {
            rgbProperties(props, vsapi);
            // vsapi->mapSetInt(props, "_ColorRange", !metadata.fullRange, maReplace); // COMPAT: Original DoViBaker bit-perfect fallback
        }

        vsapi->mapSetInt(props, "_SceneChangePrev", metadata.sceneRefresh, maReplace);

        if (!doviMetadata) return;
        vsapi->mapSetInt(props, "_dovi_dynamic_min_pq", metadata.dynamicMinPq, maReplace);
        vsapi->mapSetInt(props, "_dovi_dynamic_max_pq", metadata.dynamicMaxPq, maReplace);
        vsapi->mapSetInt(props, "_dovi_dynamic_max_content_light_level", metadata.dynamicMaxCLL, maReplace);
        vsapi->mapSetInt(props, "_dovi_static_master_display_max_luminance", metadata.staticMaxMDL, maReplace);
        vsapi->mapSetInt(props, "_dovi_static_master_display_min_luminance", metadata.staticMinMDL, maReplace);
        vsapi->mapSetInt(props, "_dovi_static_max_content_light_level", metadata.staticMaxCLL, maReplace);
        vsapi->mapSetInt(props, "_dovi_static_max_avg_content_light_level", metadata.staticMaxFALL, maReplace);
        vsapi->mapSetInt(props, "_dovi_static_max_pq", metadata.staticMaxPq, maReplace);
    }

    void setFramePropertiesRGB(VSFrame* dst, const VSAPI* vsapi) noexcept {
        VSMap* props = vsapi->getFramePropertiesRW(dst);
        vsapi->mapDeleteKey(props, "_FelBaker_YccToRGB");
        rgbProperties(props, vsapi);
    }

    RpuProcessor::YccToRgb rgbMatrix(const VSFrame* bakedSrc, const VSAPI* vsapi) {
        const VSMap* props = vsapi->getFramePropertiesRO(bakedSrc);
        if (int err = 0; const char* rpuBuf = vsapi->mapGetData(props, "_FelBaker_YccToRGB", 0, &err)) {
            return *reinterpret_cast<const RpuProcessor::YccToRgb*>(rpuBuf);
        }

        throw std::runtime_error("Ycc to RGB matrix is missing");
    }

    void convertToRGB(const VSFrame* srcY, const VSFrame* srcUV, VSFrame* dst, const RpuProcessor::YccToRgb& yccToRgb, const VSAPI* vsapi) noexcept {
        const int srcHeightUV = vsapi->getFrameHeight(srcUV, dv::Channel::U);
        const int srcWidthUV  = vsapi->getFrameWidth(srcUV, dv::Channel::U);

        const int srcStrideY  = dv::stride(vsapi->getStride(srcY, dv::Channel::Y));
        const int srcStrideUV = dv::stride(vsapi->getStride(srcUV, dv::Channel::U));
        const int dstStride   = dv::stride(vsapi->getStride(dst, dv::Channel::R));

        const auto* __restrict srcYp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(srcY, dv::Channel::Y));
        const auto* __restrict srcUp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(srcUV, dv::Channel::U));
        const auto* __restrict srcVp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(srcUV, dv::Channel::V));

        auto* __restrict dstRp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::R));
        auto* __restrict dstGp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::G));
        auto* __restrict dstBp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::B));

        for (int y = 0; y < srcHeightUV; ++y) [[likely]] {
            for (int x = 0; x < srcWidthUV; ++x) [[likely]] {
                RpuProcessor::toRGB(yccToRgb, srcYp[x], srcUp[x], srcVp[x], dstRp[x], dstGp[x], dstBp[x]);
            }

            srcYp += srcStrideY;
            srcUp += srcStrideUV;
            srcVp += srcStrideUV;

            dstRp += dstStride;
            dstGp += dstStride;
            dstBp += dstStride;
        }
    }

    template<int Is420, bool ApplyEl, bool PolynomialU, bool PolynomialV>
    void applyDovi(const VSFrame* blY, const VSFrame* blUV, const VSFrame* elY, const VSFrame* elUV, VSFrame* dst, const RpuProcessor& rpuProc, const VSAPI* vsapi) noexcept {
        const int blHeightUV = vsapi->getFrameHeight(blUV, dv::Channel::U);
        const int blWidthUV  = vsapi->getFrameWidth(blUV, dv::Channel::U);

        const int blStrideY  = dv::stride(vsapi->getStride(blY, dv::Channel::Y));
        const int blStrideUV = dv::stride(vsapi->getStride(blUV, dv::Channel::U));

        const int elStrideY  = ApplyEl ? dv::stride(vsapi->getStride(elY, dv::Channel::Y)) : 0;
        const int elStrideUV = ApplyEl ? dv::stride(vsapi->getStride(elUV, dv::Channel::U)) : 0;

        const int dstStrideY  = dv::stride(vsapi->getStride(dst, dv::Channel::Y));
        const int dstStrideUV = dv::stride(vsapi->getStride(dst, dv::Channel::U));

        const auto* __restrict blYp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(blY, dv::Channel::Y));
        const auto* __restrict blUp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(blUV, dv::Channel::U));
        const auto* __restrict blVp = reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(blUV, dv::Channel::V));

        const auto* __restrict elYp = ApplyEl ? reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(elY, dv::Channel::Y)) : nullptr;
        const auto* __restrict elUp = ApplyEl ? reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(elUV, dv::Channel::U)) : nullptr;
        const auto* __restrict elVp = ApplyEl ? reinterpret_cast<const uint16_t*>(vsapi->getReadPtr(elUV, dv::Channel::V)) : nullptr;

        auto* __restrict dstYp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::Y));
        auto* __restrict dstUp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::U));
        auto* __restrict dstVp = reinterpret_cast<uint16_t*>(vsapi->getWritePtr(dst, dv::Channel::V));

        const auto el = [](const uint16_t* elP, int idx) noexcept -> uint16_t {
            return ApplyEl ? elP[idx] : 0;
        };

        constexpr int stepY = Is420 + 1;
        for (int h = 0; h < blHeightUV; ++h) [[likely]] {
            for (int x = 0; x < blWidthUV; ++x) [[likely]] {
                const int xy = x * stepY;

                const uint16_t y = blYp[xy];
                const uint16_t u = blUp[x];
                const uint16_t v = blVp[x];

                if constexpr (Is420) {
                    const uint16_t yR  = blYp[xy + 1];
                    const uint16_t yB  = blYp[xy + blStrideY];
                    const uint16_t yBR = blYp[xy + blStrideY + 1];

                    const int xyl = xy > 0 ? xy - 1 : 0;

                    // // COMPAT: Original DoViBaker bit-perfect fallback
                    // const int xyr = x == blWidthUV - 1 ? xy : xy + 1;
                    // const int mmrYt = 2 + 2 * y  + blYp[xyl] + blYp[xyr];
                    // const int mmrYb = 2 + 2 * yB + blYp[xyl + blStrideY] + blYp[xyr + blStrideY];

                    const int mmrYt = 2 + 2 * y  + yR  + blYp[xyl];
                    const int mmrYb = 2 + 2 * yB + yBR + blYp[xyl + blStrideY];
                    const int mmrY  = ((mmrYt >> 2) + (mmrYb >> 2) + 1) >> 1;

                    dstYp[xy]                  = rpuProc.mapY<ApplyEl>(y,   el(elYp, xy));
                    dstYp[xy + 1]              = rpuProc.mapY<ApplyEl>(yR,  el(elYp, xy + 1));
                    dstYp[xy + dstStrideY]     = rpuProc.mapY<ApplyEl>(yB,  el(elYp, xy + elStrideY));
                    dstYp[xy + dstStrideY + 1] = rpuProc.mapY<ApplyEl>(yBR, el(elYp, xy + elStrideY + 1));

                    dstUp[x] = rpuProc.mapU<PolynomialU, ApplyEl>(u, el(elUp, x), mmrY, u, v);
                    dstVp[x] = rpuProc.mapV<PolynomialV, ApplyEl>(v, el(elVp, x), mmrY, u, v);
                } else {
                    dstYp[x] = rpuProc.mapY<ApplyEl>(y, el(elYp, x));
                    dstUp[x] = rpuProc.mapU<PolynomialU, ApplyEl>(u, el(elUp, x), y, u, v);
                    dstVp[x] = rpuProc.mapV<PolynomialV, ApplyEl>(v, el(elVp, x), y, u, v);
                }
            }

            blYp += blStrideY * stepY;
            blUp += blStrideUV;
            blVp += blStrideUV;

            if constexpr (ApplyEl) {
                elYp += elStrideY * stepY;
                elUp += elStrideUV;
                elVp += elStrideUV;
            }

            dstYp += dstStrideY * stepY;
            dstUp += dstStrideUV;
            dstVp += dstStrideUV;
        }
    }

}

FelBaker::FelBaker(VSNode* blNode, VSNode* elNode, bool resample, bool doviMetadata, VSCore* core, const VSAPI* vsapi) : vsapi{vsapi}, blNode{blNode}, elNode{elNode}, doviMetadata{doviMetadata} {
    const VSVideoInfo* blVi = vsapi->getVideoInfo(blNode);
    const VSVideoInfo* elVi = vsapi->getVideoInfo(elNode);

    dv::expect(blVi->numFrames == elVi->numFrames, "BL and EL must have the same frame count");
    dv::expect(blVi->format.colorFamily == cfYUV, "BL must be in YUV format");
    dv::expect(elVi->format.colorFamily == cfYUV, "EL must be in YUV format");
    dv::expect(blVi->format.subSamplingW < 2 && blVi->format.subSamplingW == blVi->format.subSamplingH, "BL subsampling must be 4:2:0 or 4:4:4");
    dv::expect(elVi->format.subSamplingW < 2 && elVi->format.subSamplingW == elVi->format.subSamplingH, "EL subsampling must be 4:2:0 or 4:4:4");

    el1080 = blVi->width != elVi->width || blVi->height != elVi->height;
    if (!resample) {
        dv::expect(blVi->format.subSamplingW == elVi->format.subSamplingW, "BL and EL subsampling must be equal");
        dv::expect(!el1080, "BL and EL must have identical dimensions");
    } else {
        dv::expect(!el1080 || (blVi->width == 2 * elVi->width && blVi->height == 2 * elVi->height), "EL must be either full or quarter resolution of BL");
    }

    blSampleBits = blVi->format.bitsPerSample;
    elSampleBits = elVi->format.bitsPerSample;
    bl420        = blVi->format.subSamplingW == 1;
    el420        = elVi->format.subSamplingW == 1;
    blFormat     = blVi->format;

    outVi = *blVi;
    if (resample || !bl420)
        vsapi->getVideoFormatByID(&outVi.format, pfRGB48, core);
    else
        vsapi->getVideoFormatByID(&outVi.format, pfYUV420P12, core);

    vsapi->getVideoFormatByID(&format444, pfYUV444P12, core);

    if ((bl420 != el420) && el1080) [[unlikely]]
        vsapi->getVideoFormatByID(&elUpFormat, el420 ? pfYUV420P12 : pfYUV444P12, core);
    else
        elUpFormat = blFormat;
}

FelBaker::FelBaker(VSNode* bakedYUV, VSCore* core, const VSAPI* vsapi) : vsapi{vsapi}, blNode{bakedYUV} {
    const VSVideoInfo* blVi = vsapi->getVideoInfo(blNode);
    dv::expect(blVi->format.colorFamily == cfYUV, "BL must be in YUV format");
    dv::expect(blVi->format.subSamplingW == 0 && blVi->format.subSamplingH == 0, "BL subsampling must be 4:4:4");

    outVi = *blVi;
    vsapi->getVideoFormatByID(&outVi.format, pfRGB48, core);
}

VSFrame* FelBaker::toRGB(int n, VSFrameContext* frameCtx, VSCore* core) const {
    const auto baked = dv::framePtr(vsapi, vsapi->getFrameFilter(n, blNode, frameCtx));

    const RpuProcessor::YccToRgb yccToRgb = rgbMatrix(baked.get(), vsapi);

    auto dst = dv::makeFrame(vsapi, &outVi.format, outVi.width, outVi.height, core, baked.get());
    setFramePropertiesRGB(dst.get(), vsapi);

    convertToRGB(baked.get(), baked.get(), dst.get(), yccToRgb, vsapi);

    return dst.release();
}

template VSFrame* FelBaker::bakeFrame<true>(int, VSFrameContext*, VSCore*) const;
template VSFrame* FelBaker::bakeFrame<false>(int, VSFrameContext*, VSCore*) const;

template<bool Resample>
VSFrame* FelBaker::bakeFrame(int n, VSFrameContext* frameCtx, VSCore* core) const {
    const auto bl = dv::framePtr(vsapi, vsapi->getFrameFilter(n, blNode, frameCtx));
    const auto el = dv::framePtr(vsapi, vsapi->getFrameFilter(n, elNode, frameCtx));

    const RpuProcessor rpuProc = initRpuProc(bl.get(), el.get());

    auto dst = dv::makeFrame(vsapi, &outVi.format, outVi.width, outVi.height, core, bl.get());

    bool frameChroma420 = bl420 && (!Resample || !rpuProc.useEl() || el420);
    dv::dispatch([&](auto frame420, auto applyEl, auto polynomialU, auto polynomialV) {
        if constexpr (frame420) {
            dv::expect(vsapi->getFrameHeight(bl.get(), dv::Channel::Y) == 2 * vsapi->getFrameHeight(bl.get(), dv::Channel::U), "BL luma height must be exactly 2x chroma for 4:2:0");
            dv::expect(vsapi->getFrameWidth(bl.get(), dv::Channel::Y) == 2 * vsapi->getFrameWidth(bl.get(), dv::Channel::U), "BL luma width must be exactly 2x chroma for 4:2:0");
        }

        setFrameProperties<!Resample && frame420>(dst.get(), rpuProc, doviMetadata, vsapi);

        if constexpr (Resample) {
            resampleAndBake<frame420, applyEl, polynomialU, polynomialV>(bl.get(), el.get(), dst.get(), rpuProc, core);
        } else if constexpr (frame420) {
            applyDovi<frame420, applyEl, polynomialU, polynomialV>(bl.get(), bl.get(), el.get(), el.get(), dst.get(), rpuProc, vsapi);
        } else {
            const auto mez = dv::makeFrame(vsapi, &blFormat, outVi.width, outVi.height, core);
            applyDovi<frame420, applyEl, polynomialU, polynomialV>(bl.get(), bl.get(), el.get(), el.get(), mez.get(), rpuProc, vsapi);
            convertToRGB(mez.get(), mez.get(), dst.get(), rpuProc.getYccToRgb(), vsapi);
        }
    }, frameChroma420, rpuProc.useEl(), rpuProc.isPolynomialU(), rpuProc.isPolynomialV());

    return dst.release();
}

template<bool Is420, bool ApplyEl, bool PolynomialU, bool PolynomialV>
void FelBaker::resampleAndBake(const VSFrame* bl, const VSFrame* el, VSFrame* dst, const RpuProcessor& rpuProc, VSCore* core) const noexcept {
    auto elUpscaled = dv::framePtr(vsapi);
    if (ApplyEl && el1080) [[likely]] {
        elUpscaled = dv::resample(el, &elUpFormat, outVi.width, outVi.height, core, vsapi);
        el         = elUpscaled.get();
    }

    if constexpr (Is420) {
        const auto mez = dv::makeFrame(vsapi, &blFormat, outVi.width, outVi.height, core);
        applyDovi<Is420, ApplyEl, PolynomialU, PolynomialV>(bl, bl, el, el, mez.get(), rpuProc, vsapi);

        const auto mez444 = dv::resampleChroma(mez.get(), &format444, outVi.width, outVi.height, core, vsapi);
        return convertToRGB(mez.get(), mez444.get(), dst, rpuProc.getYccToRgb(), vsapi);
    }

    auto bl444 = dv::framePtr(vsapi);
    auto el444 = dv::framePtr(vsapi);

    const VSVideoFormat* mezFormat = &blFormat;
    if (ApplyEl && bl420) {
        bl444     = dv::resampleChroma(bl, &format444, outVi.width, outVi.height, core, vsapi);
        mezFormat = &format444;
    } else if (ApplyEl && el420) {
        el444 = dv::resampleChroma(el, &format444, outVi.width, outVi.height, core, vsapi);
    }

    const auto mez = dv::makeFrame(vsapi, mezFormat, outVi.width, outVi.height, core);
    applyDovi<Is420, ApplyEl, PolynomialU, PolynomialV>(bl, bl444 ? bl444.get() : bl, el, el444 ? el444.get() : el, mez.get(), rpuProc, vsapi);
    convertToRGB(mez.get(), mez.get(), dst, rpuProc.getYccToRgb(), vsapi);
}

RpuProcessor FelBaker::initRpuProc(const VSFrame* blSrc, const VSFrame* elSrc) const {
    for (const VSFrame* frame : {elSrc, blSrc}) {
        const VSMap* props = vsapi->getFramePropertiesRO(frame);
        if (int err = 0; const char* rpuBuf = vsapi->mapGetData(props, "DolbyVisionRPU", 0, &err)) {
            const int rpuSize = vsapi->mapGetDataSize(props, "DolbyVisionRPU", 0, nullptr);
            return {reinterpret_cast<const uint8_t*>(rpuBuf), rpuSize, blSampleBits, elSampleBits, doviMetadata};
        }
    }

    throw std::runtime_error("RPU data is missing");
}
