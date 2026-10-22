#pragma once

#include <memory>

#include "VapourSynth4.h"

#include "VSPtr.h"

namespace dv {

    std::unique_ptr<VSFrame, VSDeleter> resample(const VSFrame* src, const VSVideoFormat* dstFmt, int width, int height, VSCore* core, const VSAPI* vsapi) noexcept;

    std::unique_ptr<VSFrame, VSDeleter> resampleChroma(const VSFrame* src, const VSVideoFormat* dstFmt, int width, int height, VSCore* core, const VSAPI* vsapi) noexcept;

}
