/* Edge brightening: per-pixel gain on a YUV420 frame, in place. Called from edge_boost.py.
 *
 * The Pi's image processor can lift the edges of a wide-angle lens by 4x at most. What is still
 * dark afterwards gets a further gain here, from a map made at calibration time.
 *
 *   boost_luma:    y = lo + (y - lo) * gain / 256, clamped to [lo, hi]   (gain in 8.8 fixed point)
 *   soften_chroma: c = 128 + (c - 128) * keep / 256   (less colour where the gain was large: the
 *                  colour of a dim area is mostly noise, and the gain would show it)
 * lo/hi are 16/235 for the video's limited range and 0/255 for full range.
 * Compile: gcc -O3 -mfpu=neon-vfpv4 -ftree-vectorize -shared -fPIC (about 8 ms per 1080p frame
 * on a Pi Zero 2 W).
 */
#include <stdint.h>

void boost_luma(uint8_t *y, int stride, int width, int height, const uint16_t *gain, int lo, int hi) {
    for (int r = 0; r < height; r++) {
        uint8_t *row = y + (long)r * stride;
        const uint16_t *g = gain + (long)r * width;
        /* No branches: that lets the compiler use the processor's vector (NEON) instructions,
           which makes this about 4x faster. Values below lo are raised to lo (legal video). */
        for (int c = 0; c < width; c++) {
            uint32_t d = row[c] > lo ? (uint32_t)(row[c] - lo) : 0u;
            uint32_t v = ((d * g[c]) >> 8) + (uint32_t)lo;
            row[c] = v > (uint32_t)hi ? (uint8_t)hi : (uint8_t)v;
        }
    }
}

/* For interleaved 3-channel stills (BGR888): every channel times the gain. */
void boost_rgb(uint8_t *p, int stride, int width, int height, const uint16_t *gain) {
    for (int r = 0; r < height; r++) {
        uint8_t *row = p + (long)r * stride;
        const uint16_t *g = gain + (long)r * width;
        for (int c = 0; c < width; c++) {
            uint32_t k = g[c];
            for (int ch = 0; ch < 3; ch++) {
                uint32_t v = ((uint32_t)row[3 * c + ch] * k) >> 8;
                row[3 * c + ch] = v > 255 ? 255 : (uint8_t)v;
            }
        }
    }
}

void soften_chroma(uint8_t *p, int stride, int width, int height, const uint8_t *keep) {
    for (int r = 0; r < height; r++) {
        uint8_t *row = p + (long)r * stride;
        const uint8_t *k = keep + (long)r * width;
        for (int c = 0; c < width; c++) {
            int v = ((int)row[c] - 128) * (int)k[c];
            row[c] = (uint8_t)(128 + (v >> 8));
        }
    }
}
