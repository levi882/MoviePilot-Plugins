#include "RpuProcessor.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>

#include "rpu_parser.h"

#include "Utils.h"

namespace {

    struct DoviDeleter final {
        void operator()(DoviRpuOpaque* p) const noexcept { dovi_rpu_free(p); }
        void operator()(const DoviRpuDataHeader* p) const noexcept { dovi_rpu_free_header(p); }
        void operator()(const DoviRpuDataMapping* p) const noexcept { dovi_rpu_free_data_mapping(p); }
        void operator()(const DoviVdrDmData* p) const noexcept { dovi_rpu_free_vdr_dm_data(p); }
    };

    [[noreturn]] void throwDoviError(const DoviRpuOpaque* rpu, std::string_view component) {
        const char* error = rpu ? dovi_rpu_get_error(rpu) : nullptr;
        throw std::runtime_error(std::string("Failed to parse RPU ").append(component).append(" - ").append(error ? error : "Unknown error"));
    }

    template<typename Factory, typename... Args>
    auto doviPtr(DoviRpuOpaque* rpu, std::string_view component, Factory&& factory, Args&&... args) {
        if (auto* ptr = std::invoke(std::forward<Factory>(factory), std::forward<Args>(args)...)) [[likely]] {
            return std::unique_ptr<std::remove_pointer_t<decltype(ptr)>, DoviDeleter>(ptr);
        }
        throwDoviError(rpu, component);
    }

    bool residualDisabled(const DoviRpuDataHeader& header) {
        if (header.guessed_profile == 7 && std::string_view(header.el_type) == "FEL") [[likely]] return header.disable_residual_flag;
        if (header.guessed_profile != 7 || std::string_view(header.el_type) == "MEL") return true; // In case of MEL we do not have any EL to work with
        throw std::runtime_error("RPU: Unknown EL type");
    }

}

RpuProcessor::RpuProcessor(const uint8_t* rpuBuf, int rpuSize, int blContainerBits, int elContainerBits, bool doviMetadata) {
    const auto rpu    = doviPtr(nullptr, "NALU", dovi_parse_unspec62_nalu, rpuBuf, rpuSize);
    const auto header = doviPtr(rpu.get(), "Header", dovi_rpu_get_header, rpu.get());

    const int coeffLog2Denom = static_cast<int>(header->coefficient_log2_denom);
    const int blBitDepth     = static_cast<int>(header->bl_bit_depth_minus8) + 8;
    const int vdrBitDepth    = static_cast<int>(header->vdr_bit_depth_minus8) + 8;

    dv::expect(blContainerBits >= blBitDepth, "Insufficient BL container bit depth");
    dv::expect(vdrBitDepth <= 12, "VDR bit depth exceeds internal 12-bit limit");

    params.blBitDepth   = blBitDepth;
    params.blShift      = blContainerBits - blBitDepth;
    params.mmrShift     = 20 - blBitDepth;
    params.mappingShift = coeffLog2Denom + 4;
    params.reconOffset  = 1 << (15 - vdrBitDepth);
    params.reconShift   = 16 - vdrBitDepth;
    params.reconLimit   = (1 << vdrBitDepth) - 1;
    params.mapOutShift  = MAP_OUT_BIT_DEPTH - vdrBitDepth;

    dv::expect(header->vdr_dm_metadata_present_flag, "RPU VDR DM metadata is missing");
    const auto vdrDm = doviPtr(rpu.get(), "VDR DM", dovi_rpu_get_vdr_dm_data, rpu.get());
    initRgbParams(*vdrDm);
    initMetadata(*vdrDm, doviMetadata);

    const auto mappingData = doviPtr(rpu.get(), "Mapping", dovi_rpu_get_data_mapping, rpu.get());
    initMappingParams(*mappingData, coeffLog2Denom);

    if ((skipEl = residualDisabled(*header))) [[unlikely]] return;

    const int elBitDepth = static_cast<int>(header->el_bit_depth_minus8) + 8;
    dv::expect(elBitDepth <= 10, "EL bit depth exceeds NLQ processor 10-bit limit");
    dv::expect(elContainerBits >= elBitDepth, "Insufficient EL container bit depth");
    params.elShift = elContainerBits - elBitDepth;

    const auto* nlq = mappingData->nlq;
    dv::expect(nlq, "RPU NLQ mapping data is missing");
    dv::expect(mappingData->nlq_method_idc == 0, "RPU NLQ: Only NLQ_LINEAR_DZ mapping is supported; NLQ_MU_LAW is undocumented"); // https://ffmpeg.org/doxygen/trunk/dovi__rpudec_8c_source.html
    dv::expect(mappingData->nlq_num_pivots_minus2 == 0, "RPU NLQ: Only 2-pivot mapping is supported");
    // alternatively, we could gracefully disable NLQ processing by setting disable_residual_flag to true for the cases above.

    initNlqLut(*nlq, elBitDepth, coeffLog2Denom);
}

void RpuProcessor::initMappingParams(const DoviRpuDataMapping& mappingData, int coeffLog2Denom) {
    for (int cmp = 0; cmp < DV_COMPONENTS; ++cmp) {
        const auto& curve      = mappingData.curves[cmp];
        const int segmentCount = static_cast<int>(curve.num_pivots_minus2) + 1;

        initPivots(curve.pivots, cmp);

        isPolynomial[cmp] = curve.mapping_idc == 0;
        if (isPolynomial[cmp]) {
            dv::expect(curve.polynomial, "RPU: Polynomial mapping data is missing");
            initPolynomialParams(*curve.polynomial, cmp, segmentCount, coeffLog2Denom);
        } else {
            dv::expect(cmp != dv::Channel::Y, "RPU: Luma (Y channel) must use polynomial mapping");
            dv::expect(curve.mmr, "RPU: MMR mapping data is missing");
            initMmrParams(*curve.mmr, cmp, segmentCount, coeffLog2Denom);
        }
    }
}

void RpuProcessor::initPivots(const DoviU16Data& pivotDeltas, int cmp) noexcept {
    std::array<uint16_t, DV_MAX_SEGMENTS + 1> pivotPoints{pivotDeltas.data[0]};
    for (size_t i = 1; i < pivotDeltas.len; ++i) {
        pivotPoints[i] = pivotPoints[i - 1] + pivotDeltas.data[i];
    }

    pivots[cmp].min = pivotPoints[0];
    pivots[cmp].max = pivotPoints[pivotDeltas.len - 1];

    int start = 0;
    for (size_t p = 0; p < pivotDeltas.len - 2; ++p) {
        const int end = std::min<int>(pivotPoints[p + 1], DV_LUTS);
        std::fill_n(&pivotLut[cmp][start], end - start, p);
        start = end;
    }
    std::fill_n(&pivotLut[cmp][start], DV_LUTS - start, pivotDeltas.len - 2);
}

void RpuProcessor::initPolynomialParams(const DoviPolynomialCurve& polynomial, int cmp, int segmentCount, int coeffLog2Denom) noexcept {
    for (int s = 0; s < segmentCount; ++s) {
        auto& segment = segments[cmp][s];
        segment.order = static_cast<int>(polynomial.poly_order_minus1.data[s]) + 1;

        const int64_t* coeffsInt   = polynomial.poly_coef_int.list[s]->data;
        const uint64_t* coeffsFrac = polynomial.poly_coef.list[s]->data;
        // an order n equation has n+1 coefficients, thus +1!
        for (int c = 0; c < segment.order + 1; ++c) {
            segment.coeffs[c] = dv::fixedPoint(coeffsInt[c], coeffsFrac[c], coeffLog2Denom) << 20;
        }
    }
}

void RpuProcessor::initMmrParams(const DoviMMRCurve& mmr, int cmp, int segmentCount, int coeffLog2Denom) noexcept {
    for (int s = 0; s < segmentCount; ++s) {
        auto& segment       = segments[cmp][s];
        segment.order       = mmr.mmr_order_minus1.data[s] + 1;
        segment.mmrConstant = dv::fixedPoint(mmr.mmr_constant_int.data[s], mmr.mmr_constant.data[s], coeffLog2Denom) << 20;

        const auto* coeffsInt  = mmr.mmr_coef_int.list[s]->list;
        const auto* coeffsFrac = mmr.mmr_coef.list[s]->list;
        for (int o = 0; o < segment.order; ++o) {
            for (int c = 0; c < 7; ++c) {
                segment.coeffs[o * 7 + c] = dv::fixedPoint(coeffsInt[o]->data[c], coeffsFrac[o]->data[c], coeffLog2Denom);
            }
        }
    }
}

void RpuProcessor::initNlqLut(const DoviRpuDataNlq& nlq, int elBitDepth, int coeffLog2Denom) noexcept {
    const int elShift    = 10 - elBitDepth;
    const int coeffShift = 10 - elBitDepth + 1;
    const int resShift   = coeffLog2Denom - 5 - elBitDepth;

    for (int cmp = 0; cmp < DV_COMPONENTS; ++cmp) {
        const int offset = nlq.nlq_offset[cmp];

        const int64_t vdrLimit  = dv::fixedPoint(nlq.vdr_in_max_int[cmp], nlq.vdr_in_max[cmp], coeffLog2Denom) << coeffShift;
        const int64_t slope     = dv::fixedPoint(nlq.linear_deadzone_slope_int[cmp], nlq.linear_deadzone_slope[cmp], coeffLog2Denom);
        const int64_t threshold = dv::fixedPoint(nlq.linear_deadzone_threshold_int[cmp], nlq.linear_deadzone_threshold[cmp], coeffLog2Denom) << coeffShift;

        const int64_t step = (2LL << elShift) * slope;

        int64_t dq = ((1 - (offset << 1)) << elShift) * slope - threshold;
        for (int el = 0; el < std::min(offset, DV_LUTS); ++el) [[likely]] {
            nlqLut[cmp][el] = static_cast<int16_t>(std::clamp(dq, -vdrLimit, vdrLimit) >> resShift);
            dq += step;
        }

        if (offset >= DV_LUTS) [[unlikely]] continue;
        nlqLut[cmp][offset] = 0;

        dq = (1LL << elShift) * slope + threshold;
        for (int el = offset + 1; el < DV_LUTS; ++el) [[likely]] {
            nlqLut[cmp][el] = static_cast<int16_t>(std::clamp(dq, -vdrLimit, vdrLimit) >> resShift);
            dq += step;
        }
    }
}

void RpuProcessor::initRgbParams(const DoviVdrDmData& vdrDm) noexcept {
    yccToRgb.coeffs = {
        vdrDm.ycc_to_rgb_coef0, vdrDm.ycc_to_rgb_coef1, vdrDm.ycc_to_rgb_coef2,
        vdrDm.ycc_to_rgb_coef3, vdrDm.ycc_to_rgb_coef4, vdrDm.ycc_to_rgb_coef5,
        vdrDm.ycc_to_rgb_coef6, vdrDm.ycc_to_rgb_coef7, vdrDm.ycc_to_rgb_coef8
    };

    yccToRgb.offsets = {
        static_cast<int>(vdrDm.ycc_to_rgb_offset0 >> YCC_TO_RGB_OFFSET_SHIFT),
        static_cast<int>(vdrDm.ycc_to_rgb_offset1 >> YCC_TO_RGB_OFFSET_SHIFT),
        static_cast<int>(vdrDm.ycc_to_rgb_offset2 >> YCC_TO_RGB_OFFSET_SHIFT)
    };
}

void RpuProcessor::initMetadata(const DoviVdrDmData& vdrDm, bool doviMetadata) noexcept {
    metadata.sceneRefresh = vdrDm.scene_refresh_flag;
    metadata.fullRange    = vdrDm.signal_full_range_flag;

    if (!doviMetadata) return;

    metadata.dynamicMinPq  = vdrDm.dm_data.level1->min_pq;
    metadata.dynamicMaxPq  = vdrDm.dm_data.level1->max_pq;
    metadata.dynamicMaxCLL = std::lround(dv::pq12ToNits(metadata.dynamicMaxPq));

    if (const auto& level6 = vdrDm.dm_data.level6) {
        metadata.staticMaxMDL  = level6->max_display_mastering_luminance;
        metadata.staticMinMDL  = level6->min_display_mastering_luminance;
        metadata.staticMaxCLL  = level6->max_content_light_level;
        metadata.staticMaxFALL = level6->max_frame_average_light_level;
        metadata.staticMaxPq   = dv::nitsToPq12(metadata.staticMaxCLL);
    }
}
