#pragma once

#include "VapourSynth4.h"

class RpuProcessor;

class alignas(64) FelBaker final {
public:
    FelBaker(VSNode* blNode, VSNode* elNode, bool resample, bool doviMetadata, VSCore* core, const VSAPI* vsapi);
    FelBaker(VSNode* bakedYUV, VSCore* core, const VSAPI* vsapi);

    ~FelBaker() noexcept {
        vsapi->freeNode(blNode);
        if (elNode) vsapi->freeNode(elNode);
    }

    template<bool RequestEl>
    void requestFrames(int n, VSFrameContext* frameCtx) const noexcept {
        vsapi->requestFrameFilter(n, blNode, frameCtx);
        if constexpr (RequestEl) vsapi->requestFrameFilter(n, elNode, frameCtx);
    }

    [[nodiscard]] const VSVideoInfo& getOutVi() const noexcept { return outVi; }

    VSFrame* toRGB(int n, VSFrameContext* frameCtx, VSCore* core) const;

    template<bool Resample>
    VSFrame* bakeFrame(int n, VSFrameContext* frameCtx, VSCore* core) const;

private:
    VSVideoInfo outVi{};
    const VSAPI* vsapi;
    VSNode* blNode;
    VSNode* elNode{};
    int blSampleBits{};
    int elSampleBits{};
    bool doviMetadata{};
    bool bl420{};
    bool el420{};
    bool el1080{};
    VSVideoFormat blFormat{};
    VSVideoFormat elUpFormat{};
    VSVideoFormat format444{};

    template<bool Is420, bool ApplyEl, bool PolynomialU, bool PolynomialV>
    void resampleAndBake(const VSFrame* bl, const VSFrame* el, VSFrame* dst, const RpuProcessor& rpuProc, VSCore* core) const noexcept;

    RpuProcessor initRpuProc(const VSFrame* blSrc, const VSFrame* elSrc) const;
};
