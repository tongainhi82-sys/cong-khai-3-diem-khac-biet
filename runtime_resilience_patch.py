#!/usr/bin/env python3
"""Runtime-patch the private generator for quality-preserving fallbacks and skippable games."""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"Runtime resilience patch failed: {label} changed")
    return text.replace(old, new, 1)


def patch_quality_diversity(root: Path) -> None:
    path = root / "quality_diversity_runner.py"
    text = path.read_text(encoding="utf-8")
    sentinel = "# === QUALITY-ONLY FINAL-SCENE RESILIENCE ==="
    if sentinel in text:
        print("Quality-only resilience patch already present")
        return

    marker = "def patch_manifest_quality_diversity() -> None:\n"
    if marker not in text:
        raise SystemExit("Runtime resilience patch failed: quality-diversity manifest marker changed")

    block = r'''# === QUALITY-ONLY FINAL-SCENE RESILIENCE ===
def _quality_only_boxes_overlap(a, b, pad: int = 6) -> bool:
    ax0, ay0, ax1, ay1 = map(int, a)
    bx0, by0, bx1, by1 = map(int, b)
    return not (
        ax1 + pad <= bx0
        or bx1 + pad <= ax0
        or ay1 + pad <= by0
        or by1 + pad <= ay0
    )


def _create_pair_quality_only_final(base: Image.Image, seed: int):
    """Ignore scene-level group/spread constraints but keep normal per-edit QA mandatory."""
    candidates = cpuopt.detect_prefiltered_candidates(base)
    if len(candidates) < 3:
        raise RuntimeError(
            f"Quality-only final mode found only {len(candidates)}/3 quality candidates"
        )

    edited = base.copy()
    regions: list[dict[str, object]] = []
    metadata: list[dict[str, object]] = []
    accepted: list[dict[str, object]] = []
    pending = list(candidates)
    attempted = 0
    original_plan = strict.plan_edit

    # Use the normal diverse planner rather than the mandatory grouped planner.
    # The renderer still enforces pixel, clean-difference, structure and semantic QA.
    strict.plan_edit = cpuopt._ORIGINAL_PLAN
    try:
        while len(accepted) < 3 and attempted < core.MAX_INPAINT_CANDIDATES:
            eligible = []
            for pending_index, item in enumerate(pending):
                item_box = _key(item)
                if any(
                    _quality_only_boxes_overlap(item_box, _key(old), pad=4)
                    for old in accepted
                ):
                    continue

                priority = (
                    float(item.get("renderability_score", 0.0)) * 2.5
                    + float(item.get("quality", 0.0)) * 0.08
                    + float(item.get("category_confidence", 0.0)) * 2.0
                )
                eligible.append((priority, pending_index, item))

            if not eligible:
                break

            eligible.sort(key=lambda row: row[0], reverse=True)
            selected_index = eligible[0][1]
            original = pending.pop(selected_index)
            attempted += 1
            number = len(accepted) + 1
            item = dict(original)
            item["difference_slot"] = number

            print(
                f"[quality-only] candidate {attempted}: slot={number} "
                f"category={item.get('category')} "
                f"render={float(item.get('renderability_score', 0.0)):.2f} "
                f"bbox={item.get('bbox')}"
            )

            try:
                candidate, meta = strict.diffusers_edit_closed_set(
                    edited,
                    item,
                    seed + number * 104729 + attempted * 4099,
                    number,
                )
            except RuntimeError as exc:
                print(
                    f"[quality-only] quality candidate rejected "
                    f"({attempted}/{core.MAX_INPAINT_CANDIDATES}): {exc}"
                )
                continue

            x0, y0, x1, y1 = map(int, meta["pixel_qa"]["changed_bbox"])
            changed_box = (x0, y0, x1, y1)
            if any(
                _quality_only_boxes_overlap(changed_box, tuple(r["changed_bbox"]), pad=6)
                for r in regions
            ):
                print("[quality-only] changed region overlaps an accepted difference; reject")
                continue

            plan = meta["closed_set_plan"]
            rx, ry = (x0 + x1) // 2, (y0 + y1) // 2
            radius = max(18, int(math.hypot(x1 - x0, y1 - y0) * 0.54))
            actual_group = str(
                plan.get("difference_group", _edit_group(str(plan["edit_type"])))
            )

            edited = candidate
            accepted.append(item)
            metadata.append(meta)
            regions.append(
                {
                    "number": number,
                    "x": rx,
                    "y": ry,
                    "r": radius,
                    "bbox": list(item["bbox"]),
                    "changed_bbox": [x0, y0, x1, y1],
                    "area_ratio": round(float(item["ratio"]), 5),
                    "category": item["category"],
                    "edit_type": plan["edit_type"],
                    "difference_group": actual_group,
                    "target_part": plan["target_part"],
                    "single_object": True,
                    "noncentral": True,
                    "renderability_score": round(float(item.get("renderability_score", 0.0)), 4),
                    "quality_only_final_mode": True,
                }
            )
    finally:
        strict.plan_edit = original_plan

    if len(regions) != 3:
        raise RuntimeError(
            f"Quality-only final mode accepted only {len(regions)}/3 differences "
            f"after {attempted} candidates; quality QA remains mandatory"
        )

    print("[quality-only] accepted 3/3 differences with normal quality QA")
    return edited, regions, metadata


_STRICT_GROUPED_PAIR = create_pair_grouped


def create_pair_grouped(base: Image.Image, seed: int):
    """Normal grouped mode; final scene falls back to QA-only, then one extra scene regeneration."""
    provider_meta = getattr(strict, "_LAST_PROVIDER_META", None) or {}
    regen_attempt = int(provider_meta.get("scene_regeneration_attempt", 0) or 0)
    final_regen = (
        strict.SCENE_REGEN_RETRIES > 0
        and regen_attempt >= strict.SCENE_REGEN_RETRIES
    )

    if not final_regen:
        return _STRICT_GROUPED_PAIR(base, seed)

    original_spread = scene.MIN_SPREAD
    spread_levels = [original_spread]
    for fallback_spread in (0.21, 0.19):
        if fallback_spread < spread_levels[-1]:
            spread_levels.append(fallback_spread)

    grouped_errors = []
    try:
        for min_spread in spread_levels:
            scene.MIN_SPREAD = min_spread
            try:
                result = _STRICT_GROUPED_PAIR(base, seed)
            except RuntimeError as exc:
                grouped_errors.append(f"{min_spread:.2f}: {exc}")
                print(
                    f"[quality-only] grouped mode failed at spread "
                    f"{min_spread:.2f}: {exc}"
                )
                if "structural-detail candidate survived prefilter" in str(exc):
                    break
                continue

            if min_spread < original_spread:
                print(
                    f"[diversity] final-scene spread fallback active: "
                    f"{original_spread:.2f} -> {min_spread:.2f}"
                )
            return result

        scene.MIN_SPREAD = original_spread
        print(
            "[quality-only] grouped constraints exhausted; "
            "dropping spread/group requirements while keeping all edit QA"
        )
        try:
            return _create_pair_quality_only_final(base, seed)
        except RuntimeError as first_quality_error:
            print(
                "[quality-only] first QA-only attempt failed; "
                f"regenerating one additional scene: {first_quality_error}"
            )

        # One additional scene is allowed beyond SCENE_REGEN_RETRIES. This keeps
        # quality gates intact instead of weakening semantic/visual QA.
        extra_retry = strict.SCENE_REGEN_RETRIES + 1
        strict._regenerate_scene(base, extra_retry)
        try:
            result = _create_pair_quality_only_final(base, seed + extra_retry * 1000003)
            print("[quality-only] extra regenerated scene passed 3/3 mandatory QA")
            return result
        except RuntimeError as second_quality_error:
            raise RuntimeError(
                "Final quality-only mode failed after one extra scene regeneration: "
                f"{second_quality_error}; grouped attempts={grouped_errors}"
            ) from second_quality_error
    finally:
        scene.MIN_SPREAD = original_spread


'''

    path.write_text(text.replace(marker, block + marker, 1), encoding="utf-8")
    print("Patched quality-diversity: 0.23 -> 0.21 -> 0.19 -> QA-only -> one extra regenerated QA-only scene")


def patch_core_skip_failed_games(root: Path) -> None:
    path = root / "difference_forge_cf.py"
    text = path.read_text(encoding="utf-8")
    sentinel = '"skipped_games": skipped_games,'
    if sentinel in text:
        print("Skippable-game core patch already present")
        return

    old_loop = '''    pairs = []
    records = []

    for game, (raw, theme) in enumerate(zip(chosen, themes, strict=True), 1):
        prompt = build_game_prompt(raw, theme, game)
        print(f"[game {game}/{GAME_COUNT}] theme={theme}")
        base, provider_meta = generate_base(prompt)
        base = enhance_base(base)
        base.save(generated_dir / f"game-{game:02d}-base.jpg", "JPEG", quality=95)
        edited, regions, edit_meta = create_difference_pair(base, seed + game * 104729)
        edited.save(generated_dir / f"game-{game:02d}-edited.jpg", "JPEG", quality=95)
        pairs.append((base, edited, regions))
        records.append({
            "game": game,
            "theme": theme,
            "prompt": prompt,
            "base_image_provider": provider_meta,
            "remote_usage": "base-image-generation-only",
            "regions": regions,
            "diffusers_inpainting": edit_meta,
        })

    video, thumbnail, timeline = render_video(pairs, themes, title, titles)
'''

    new_loop = '''    pairs = []
    records = []
    successful_themes = []
    skipped_games = []
    requested_game_count = GAME_COUNT

    for source_game, (raw, theme) in enumerate(zip(chosen, themes, strict=True), 1):
        prompt = build_game_prompt(raw, theme, source_game)
        print(f"[game {source_game}/{requested_game_count}] theme={theme}")
        try:
            base, provider_meta = generate_base(prompt)
            base = enhance_base(base)
            base.save(generated_dir / f"game-{source_game:02d}-base.jpg", "JPEG", quality=95)
            edited, regions, edit_meta = create_difference_pair(
                base, seed + source_game * 104729
            )
            edited.save(
                generated_dir / f"game-{source_game:02d}-edited.jpg", "JPEG", quality=95
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            skipped_games.append({
                "source_game": source_game,
                "theme": theme,
                "prompt": prompt,
                "error": error,
            })
            print(
                f"[game {source_game}/{requested_game_count}] SKIPPED after all quality retries: {error}"
            )
            continue

        output_game = len(pairs) + 1
        pairs.append((base, edited, regions))
        successful_themes.append(theme)
        records.append({
            "game": output_game,
            "source_game": source_game,
            "theme": theme,
            "prompt": prompt,
            "base_image_provider": provider_meta,
            "remote_usage": "base-image-generation-only",
            "regions": regions,
            "diffusers_inpainting": edit_meta,
        })
        print(
            f"[game {source_game}/{requested_game_count}] accepted as video game {output_game}"
        )

    if not pairs:
        raise RuntimeError("All requested games failed quality generation; no puzzle game is available for video")

    # Render and score using only successful games. The displayed numbering is
    # contiguous (1..N) even when one or more source games were skipped.
    globals()["GAME_COUNT"] = len(pairs)
    if hasattr(presentation.get_style, "cache_clear"):
        presentation.get_style.cache_clear()
    print(
        f"[games] requested={requested_game_count} completed={len(pairs)} "
        f"skipped={len(skipped_games)}"
    )
    video, thumbnail, timeline = render_video(pairs, successful_themes, title, titles)
'''

    text = replace_once(text, old_loop, new_loop, "core game loop")

    old_manifest = '''        "games": records,
        "timeline": timeline,
        "score_max": GAME_COUNT * 3,
'''
    new_manifest = '''        "games": records,
        "skipped_games": skipped_games,
        "requested_game_count": requested_game_count,
        "completed_game_count": GAME_COUNT,
        "timeline": timeline,
        "score_max": GAME_COUNT * 3,
'''
    text = replace_once(text, old_manifest, new_manifest, "core manifest game summary")

    path.write_text(text, encoding="utf-8")
    print("Patched core main: failed games are skipped; successful games are renumbered and rendered")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "private-source").resolve()
    if not root.is_dir():
        raise SystemExit(f"Private source directory not found: {root}")
    patch_quality_diversity(root)
    patch_core_skip_failed_games(root)


if __name__ == "__main__":
    main()
