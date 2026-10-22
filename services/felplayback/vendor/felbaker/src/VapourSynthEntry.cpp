#include "VapourSynth4.h"

#include <array>
#include <exception>
#include <string>

#include "FelBaker.h"

namespace {

    template<auto BakeFn, bool RequestEl = true>
    const VSFrame* getBakedFrame(int n, int reason, void* instanceData, VSFrameContext* frameCtx, VSCore* core, const VSAPI* vsapi) noexcept {
        const auto* felBaker = static_cast<const FelBaker*>(instanceData);

        if (reason == arInitial) {
            felBaker->requestFrames<RequestEl>(n, frameCtx);
        } else if (reason == arAllFramesReady) {
            try {
                return (felBaker->*BakeFn)(n, frameCtx, core);
            } catch (const std::exception& e) {
                vsapi->setFilterError(("FelBaker: " + std::string(e.what())).c_str(), frameCtx);
            }
        }

        return nullptr;
    }

    const VSFrame* VS_CC getBaked(int n, int reason, void* instanceData, void**, VSFrameContext* frameCtx, VSCore* core, const VSAPI* vsapi) noexcept {
        return getBakedFrame<&FelBaker::bakeFrame<true>>(n, reason, instanceData, frameCtx, core, vsapi);
    }

    const VSFrame* VS_CC getBakedNoResample(int n, int reason, void* instanceData, void**, VSFrameContext* frameCtx, VSCore* core, const VSAPI* vsapi) noexcept {
        return getBakedFrame<&FelBaker::bakeFrame<false>>(n, reason, instanceData, frameCtx, core, vsapi);
    }

    const VSFrame* VS_CC getRGB(int n, int reason, void* instanceData, void**, VSFrameContext* frameCtx, VSCore* core, const VSAPI* vsapi) noexcept {
        return getBakedFrame<&FelBaker::toRGB, false>(n, reason, instanceData, frameCtx, core, vsapi);
    }

    void VS_CC destroyFelBaker(void* instanceData, VSCore*, const VSAPI*) noexcept {
        delete static_cast<FelBaker*>(instanceData);
    }

    template<typename Factory, typename... Nodes>
    void createFilter(const char* filterName, Factory&& factory, const VSFilterGetFrame getFrame, const VSMap* in, VSMap* out, VSCore* core, const VSAPI* vsapi, Nodes... nodes) noexcept {
        FelBaker* felBaker = nullptr;
        try {
            felBaker = std::forward<Factory>(factory)();
        } catch (const std::exception& e) {
            (vsapi->freeNode(nodes), ...);
            return vsapi->mapSetError(out, ("FelBaker: " + std::string(e.what())).c_str());
        }

        int error = 0;
        const int filterMode = vsapi->mapGetInt(in, "non_parallel", 0, &error) ? fmUnordered : fmParallel;

        const std::array<VSFilterDependency, sizeof...(Nodes)> deps = {
            VSFilterDependency{.source = nodes, .requestPattern = rpStrictSpatial}...
        };

        vsapi->createVideoFilter(out, filterName, &felBaker->getOutVi(), getFrame, destroyFelBaker, filterMode, deps.data(), deps.size(), felBaker, core);
    }

    void VS_CC bakeFilter(const VSMap* in, VSMap* out, void*, VSCore* core, const VSAPI* vsapi) noexcept {
        int error = 0;
        const bool doviMetadata = vsapi->mapGetInt(in, "dovi_metadata", 0, &error);
        const bool noResample   = vsapi->mapGetInt(in, "no_resample", 0, &error);

        VSNode* blNode = vsapi->mapGetNode(in, "bl", 0, nullptr);
        VSNode* elNode = vsapi->mapGetNode(in, "el", 0, nullptr);

        const VSFilterGetFrame getFrame = noResample ? getBakedNoResample : getBaked;
        auto factory = [&] { return new FelBaker(blNode, elNode, !noResample, doviMetadata, core, vsapi); };
        createFilter("Bake", factory, getFrame, in, out, core, vsapi, blNode, elNode);
    }

    void VS_CC rgbFilter(const VSMap* in, VSMap* out, void*, VSCore* core, const VSAPI* vsapi) noexcept {
        VSNode* bakedNode = vsapi->mapGetNode(in, "baked", 0, nullptr);

        auto factory = [&] { return new FelBaker(bakedNode, core, vsapi); };
        createFilter("ToRGB", factory, getRGB, in, out, core, vsapi, bakedNode);
    }

}

VS_EXTERNAL_API(void) VapourSynthPluginInit2(VSPlugin* plugin, const VSPLUGINAPI* pApi) noexcept {
    pApi->configPlugin("com.vapoursynth.felbaker", "fel", "FelBaker", VS_MAKE_VERSION(1, 0), VAPOURSYNTH_API_VERSION, 0, plugin);
    pApi->registerFunction("Bake", "bl:vnode;el:vnode;no_resample:int:opt;dovi_metadata:int:opt;non_parallel:int:opt", "clip:vnode;", bakeFilter, nullptr, plugin);
    pApi->registerFunction("ToRGB", "baked:vnode;non_parallel:int:opt", "clip:vnode;", rgbFilter, nullptr, plugin);
}
