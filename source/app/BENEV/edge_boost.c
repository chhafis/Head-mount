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
#include <stdlib.h>

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

/* Gain with noise reduction that grows with the gain. A dim area that is lifted 6x has six times its
 * noise, and no gain alone can tell picture from noise, so where the gain is large the pixel is
 * mixed with the average of its (2R+1)^2 neighbours before the gain is applied (what lens shading
 * correction in camera ISPs does, they pair the gain with stronger denoising there).
 *
 *   mix[]   0..255 per pixel: how much of the neighbourhood average replaces the pixel
 *   gain[]  8.8 fixed point (luma / colour channels), or keep[] 0..256 (chroma planes: colour kept)
 * The average is taken in 8.8 fixed point, so lifting a value of 3 by 8 does not step by 8.
 * step: bytes between pixels (1 for a plane, 3 for one channel of BGR888).
 * Returns 0, or -1 if memory ran out (the plane is then left unchanged). */
static int plane_nr(uint8_t *p, int stride, int step, int w, int h, int R, const uint8_t *mix,
                    const uint16_t *gain, const uint8_t *keep, int lo, int hi) {
    int n = 2 * R + 1;
    float scale = 256.0f / ((float)n * (float)n);
    uint16_t *tmp = (uint16_t *)malloc((size_t)w * h * sizeof(uint16_t));
    uint32_t *col = (uint32_t *)calloc((size_t)w, sizeof(uint32_t));
    if (!tmp || !col) { free(tmp); free(col); return -1; }
    for (int r = 0; r < h; r++) {                      /* horizontal sums */
        const uint8_t *row = p + (long)r * stride;
        uint16_t *t = tmp + (long)r * w;
        uint32_t s = 0;
        for (int k = -R; k <= R; k++) {
            int i = k < 0 ? 0 : (k > w - 1 ? w - 1 : k);
            int v = row[i * step];
            s += v > lo ? (uint32_t)(v - lo) : 0u;
        }
        for (int c = 0; c < w; c++) {
            t[c] = (uint16_t)s;
            int a = c + R + 1; a = a > w - 1 ? w - 1 : a;
            int b = c - R;     b = b < 0 ? 0 : b;
            int va = row[a * step], vb = row[b * step];
            s += (va > lo ? (uint32_t)(va - lo) : 0u);
            s -= (vb > lo ? (uint32_t)(vb - lo) : 0u);
        }
    }
    for (int k = -R; k <= R; k++) {                    /* vertical sums, sliding down the rows */
        const uint16_t *t = tmp + (long)(k < 0 ? 0 : (k > h - 1 ? h - 1 : k)) * w;
        for (int c = 0; c < w; c++) col[c] += t[c];
    }
    for (int r = 0; r < h; r++) {
        uint8_t *row = p + (long)r * stride;
        const uint8_t *m = mix + (long)r * w;
        for (int c = 0; c < w; c++) {
            int v = row[c * step];
            int d = v > lo ? v - lo : 0;
            int d_fp = d << 8;
            int blur = (int)((float)col[c] * scale);
            int mixed = d_fp + (((blur - d_fp) * (int)m[c]) >> 8);
            if (mixed < 0) mixed = 0;
            int out;
            if (gain) {
                uint32_t o = (((uint32_t)mixed * gain[(long)r * w + c]) >> 16) + (uint32_t)lo;
                out = o > (uint32_t)hi ? hi : (int)o;
            } else {
                out = 128 + (((mixed - 32768) * (int)keep[(long)r * w + c]) >> 16);
                out = out < 0 ? 0 : (out > 255 ? 255 : out);
            }
            row[c * step] = (uint8_t)out;
        }
        if (r + 1 < h) {                               /* slide the vertical window */
            int a = r + R + 1; a = a > h - 1 ? h - 1 : a;
            int b = r - R;     b = b < 0 ? 0 : b;
            const uint16_t *ta = tmp + (long)a * w, *tb = tmp + (long)b * w;
            for (int c = 0; c < w; c++) col[c] += (uint32_t)ta[c] - (uint32_t)tb[c];
        }
    }
    free(tmp); free(col);
    return 0;
}

int boost_luma_nr(uint8_t *y, int stride, int w, int h, const uint16_t *gain, const uint8_t *mix, int R, int lo, int hi) {
    return plane_nr(y, stride, 1, w, h, R, mix, gain, 0, lo, hi);
}

int soften_chroma_nr(uint8_t *p, int stride, int w, int h, const uint8_t *keep, const uint8_t *mix, int R) {
    return plane_nr(p, stride, 1, w, h, R, mix, 0, keep, 0, 255);
}

int boost_rgb_nr(uint8_t *p, int stride, int w, int h, const uint16_t *gain, const uint8_t *mix, int R) {
    int rc = 0;
    for (int ch = 0; ch < 3; ch++) rc |= plane_nr(p + ch, stride, 3, w, h, R, mix, gain, 0, 0, 255);
    return rc;
}

/* BGR888 stills: less colour where the gain was large (keep: 0..256 at half resolution), as
 * soften_chroma does for the video's chroma planes: pixel = grey + (pixel - grey) * keep / 256. */
void desaturate_rgb(uint8_t *p, int stride, int w, int h, const uint8_t *keep, int keep_w) {
    for (int r = 0; r < h; r++) {
        uint8_t *row = p + (long)r * stride;
        const uint8_t *k = keep + (long)(r >> 1) * keep_w;
        for (int c = 0; c < w; c++) {
            int kk = k[c >> 1];
            if (kk >= 256 - 1) continue;
            int b = row[3 * c], g = row[3 * c + 1], rr = row[3 * c + 2];
            int grey = (29 * b + 150 * g + 77 * rr) >> 8;
            for (int ch = 0; ch < 3; ch++) {
                int v = grey + (((int)row[3 * c + ch] - grey) * kk >> 8);
                row[3 * c + ch] = (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v));
            }
        }
    }
}
