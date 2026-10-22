FROM archlinux:base-20261018.0.612668 AS runtime
RUN pacman -Syu --noconfirm --needed python vapoursynth ffms2 ffmpeg libdovi intel-media-driver vpl-gpu-rt mkvtoolnix-cli \
    && pacman -Q > /package-versions.txt \
    && pacman -Scc --noconfirm

FROM runtime AS builder
RUN pacman -Syu --noconfirm --needed gcc pkgconf
COPY vendor/felbaker /src
RUN g++ -shared -fPIC -std=c++20 -O3 -march=x86-64-v3 \
    -I/src/src -I/usr/include/libdovi \
    /src/src/VapourSynthEntry.cpp /src/src/FelBaker.cpp \
    /src/src/RpuProcessor.cpp /src/src/Spline16Resampler.cpp \
    $(pkg-config --cflags --libs vapoursynth dovi) -o /libfelbaker.so

FROM runtime
COPY --from=builder /libfelbaker.so /opt/gb1/libfelbaker.so
COPY vendor/felbaker /opt/gb1/source/felbaker
COPY fetch_dovi.py /opt/gb1/build/fetch_dovi.py
RUN python /opt/gb1/build/fetch_dovi.py && rm -rf /opt/gb1/build
COPY felworker /opt/gb1/felworker
RUN chmod -R u=rwX,go=rX /opt/gb1
ENV PYTHONPATH=/opt/gb1 HOME=/tmp LIBVA_DRIVER_NAME=iHD PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "felworker.worker"]
