#pragma once

#include <memory>

#include "VapourSynth4.h"

namespace dv {

    struct VSDeleter final {
        const VSAPI* vsapi;
        void operator()(const VSFrame* frame) const noexcept { if (frame) vsapi->freeFrame(frame); }
    };

    template<typename T = VSFrame>
    [[nodiscard]] auto framePtr(const VSAPI* vsapi, T* resource = nullptr) noexcept {
        return std::unique_ptr<T, VSDeleter>(resource, VSDeleter{vsapi});
    }

    [[nodiscard]] inline auto makeFrame(const VSAPI* vsapi, const VSVideoFormat* format, int width, int height, VSCore* core, const VSFrame* propSrc = nullptr) noexcept {
        return framePtr(vsapi, vsapi->newVideoFrame(format, width, height, propSrc, core));
    }

}
