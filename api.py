ad_countdown_txt = ft.Text("Ad: 15s remaining", size=13, weight="bold", color="black")
    ad_title_txt = ft.Text(state["ad_title"], size=18, weight="bold", color="white", text_align=ft.TextAlign.CENTER)
    ad_sub_txt = ft.Text(state["ad_banner_text"], size=13, color=TEXT_DIM, text_align=ft.TextAlign.CENTER)

    ad_video_player = ft.Video(
        expand=True,
        playlist=[ft.VideoMedia(state["ad_video_url"])],
        playlist_mode=ft.PlaylistMode.SINGLE,
        fill_color="black",
        aspect_ratio=16 / 9,
        autoplay=False,
        show_controls=False,
    )

    ad_overlay_view = ft.Container(
        content=ft.SafeArea(
            ft.Column([
                ft.Row([
                    ft.Container(
                        content=ad_countdown_txt,
                        padding=ft.padding.symmetric(horizontal=14, vertical=6),
                        border_radius=12,
                        bgcolor=APP_COLOR,
                    ),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(
                    content=ad_video_player,
                    height=320,
                    border_radius=20,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    border=ft.border.all(1.5, APP_COLOR),
                ),
                ad_title_txt,
                ad_sub_txt,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER, spacing=14),
            expand=True,
        ),
        bgcolor=W(0.96, "#050A0E"),
        padding=20,
        left=0, right=0, top=0, bottom=0,
        visible=False,
    )

    def trigger_mid_song_video_ad():
        if state["ad_playing"] or state["is_premium"] or not state["ads_enabled"]:
            return
        state["ad_playing"] = True
        saved_pos_ms = int(state["current_position"] * 1000)

        # 1. Chal rahe 1-2 ghante wale gaane ko pause karein
        try:
            audio_player.pause()
        except Exception:
            pass
        state["is_playing"] = False
        set_play_icon(I("PLAY_ARROW_ROUNDED"))

        # 2. Video Ad Screen open karein aur Video play karein
        ad_title_txt.value = state["ad_title"]
        ad_sub_txt.value = state["ad_banner_text"]
        try:
            ad_video_player.playlist = [ft.VideoMedia(state["ad_video_url"])]
        except Exception:
            pass
        ad_overlay_view.visible = True
        page.update()

        try:
            ad_video_player.play()
        except Exception:
            pass

        # 3. Countdown chalayein aur khatam hone par gaana wahi se Resume karein
        dur = max(5, int(state.get("ad_duration", 15)))
        for rem in range(dur, 0, -1):
            if not state["alive"]:
                return
            ad_countdown_txt.value = f"Sponsored Video • {rem}s"
            try:
                ad_countdown_txt.update()
            except Exception:
                pass
            time.sleep(1)

        try:
            ad_video_player.pause()
        except Exception:
            pass
        ad_overlay_view.visible = False
        state["ad_playing"] = False

        # 4. Gaana wahi exact second se wapas Resume!
        try:
            audio_player.seek(saved_pos_ms)
            audio_player.resume()
        except Exception:
            pass
        state["is_playing"] = True
        set_play_icon(I("PAUSE_ROUNDED"))
        page.update()
