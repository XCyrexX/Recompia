// SPDX-License-Identifier: GPL-3.0-or-later
// Dear ImGui overlay (declarations in include/nxrt/overlay.h). ImGui and its Vulkan backend are pinned in cmake/gfx_deps.cmake.
#include "overlay_impl.h"

#include "nxrt/host_files.h"
#include "nxrt/notices.h"

namespace nxrt::gfx {
using namespace overlay_detail;
namespace {

// The overlay's text (the on-screen keyboard, the quit prompt, the F1 page, the HUD) in M PLUS Rounded 1c Medium (SIL OFL 1.1,
// runtime/assets/fonts), a rounded face nearer the game's own text than ImGui's: Latin, Greek, Cyrillic, kana, about 5,000 kanji.
// Since ImGui 1.92 the sources merged into one font are asked in order for a glyph (docs/FONTS.md), so DejaVu Sans (the release's
// shared-font fallback, src/services/pl.cpp) answers what M PLUS lacks and the built-in vector font the rest; with neither file
// the built-in font is the whole font, as before.
void add_fonts(ImGuiIO& io) {
    ImFontConfig cfg;
    for (const char* name : {"MPLUSRounded1c-Medium.ttf", "DejaVuSans.ttf"}) {
        const std::filesystem::path p = host_asset("fonts", name);
        if (p.empty()) continue;
        const std::u8string u8 = p.u8string();   // ImGui opens files by UTF-8 name, also on Windows
        if (io.Fonts->AddFontFromFileTTF(reinterpret_cast<const char*>(u8.c_str()), 0.0f, &cfg)) cfg.MergeMode = true;
        else NXLOG_W(kTag, "overlay font %s could not be loaded", p.string().c_str());
    }
    io.Fonts->AddFontDefaultVector(&cfg);   // the scalable built-in font: stays sharp at every UI scale (update_scale)
}

ImGuiKey map_key(int sc) {
    if (sc >= SDL_SCANCODE_A && sc <= SDL_SCANCODE_Z) return static_cast<ImGuiKey>(ImGuiKey_A + (sc - SDL_SCANCODE_A));
    if (sc >= SDL_SCANCODE_1 && sc <= SDL_SCANCODE_9) return static_cast<ImGuiKey>(ImGuiKey_1 + (sc - SDL_SCANCODE_1));
    if (sc == SDL_SCANCODE_0) return ImGuiKey_0;
    if (sc >= SDL_SCANCODE_F1 && sc <= SDL_SCANCODE_F12) return static_cast<ImGuiKey>(ImGuiKey_F1 + (sc - SDL_SCANCODE_F1));
    switch (sc) {
        case SDL_SCANCODE_RETURN: return ImGuiKey_Enter;
        case SDL_SCANCODE_ESCAPE: return ImGuiKey_Escape;
        case SDL_SCANCODE_BACKSPACE: return ImGuiKey_Backspace;
        case SDL_SCANCODE_TAB: return ImGuiKey_Tab;
        case SDL_SCANCODE_SPACE: return ImGuiKey_Space;
        case SDL_SCANCODE_INSERT: return ImGuiKey_Insert;
        case SDL_SCANCODE_DELETE: return ImGuiKey_Delete;
        case SDL_SCANCODE_HOME: return ImGuiKey_Home;
        case SDL_SCANCODE_END: return ImGuiKey_End;
        case SDL_SCANCODE_PAGEUP: return ImGuiKey_PageUp;
        case SDL_SCANCODE_PAGEDOWN: return ImGuiKey_PageDown;
        case SDL_SCANCODE_RIGHT: return ImGuiKey_RightArrow;
        case SDL_SCANCODE_LEFT: return ImGuiKey_LeftArrow;
        case SDL_SCANCODE_DOWN: return ImGuiKey_DownArrow;
        case SDL_SCANCODE_UP: return ImGuiKey_UpArrow;
        case SDL_SCANCODE_LCTRL: return ImGuiKey_LeftCtrl;
        case SDL_SCANCODE_RCTRL: return ImGuiKey_RightCtrl;
        case SDL_SCANCODE_LSHIFT: return ImGuiKey_LeftShift;
        case SDL_SCANCODE_RSHIFT: return ImGuiKey_RightShift;
        case SDL_SCANCODE_LALT: return ImGuiKey_LeftAlt;
        case SDL_SCANCODE_RALT: return ImGuiKey_RightAlt;
        default: return ImGuiKey_None;
    }
}

}  // namespace

namespace overlay_detail {
// Drops pressed keys and mouse buttons so nothing stays latched in ImGui (the caller holds the context's mutex).
void clear_imgui_input(ImGuiContext* ctx) {
    ImGuiContext* prev = ImGui::GetCurrentContext();
    ImGui::SetCurrentContext(ctx);
    ImGuiIO& io = ImGui::GetIO();
    io.ClearInputKeys();
    io.ClearInputMouse();
    ImGui::SetCurrentContext(prev);
}
}  // namespace overlay_detail

namespace {
std::atomic<int> g_shader_backlog{0};
}
void set_shader_backlog(int pending) { g_shader_backlog.store(pending < 0 ? 0 : pending, std::memory_order_relaxed); }
int shader_backlog() { return g_shader_backlog.load(std::memory_order_relaxed); }

// DPI: the UI is laid out for a 1080 pixel high frame at 16 px text and grows with the frame (4K: twice the size; a Windows display scaling
// of 150% shows up as more pixels in the same window, so it is covered too). Never below 1: a 720p window keeps the 16 px text.
void Overlay::Impl::update_scale(const VkExtent2D& extent) {
    const float want = std::round(std::clamp(static_cast<float>(extent.height) / 1080.f, 1.0f, 3.0f) * 20.f) / 20.f;   // steps of 0.05
    if (want == ui_scale) return;
    ui_scale = want;
    ImGuiStyle& style = ImGui::GetStyle();
    style = base_style;
    style.ScaleAllSizes(want);
    style.FontScaleDpi = want;
}

std::unique_ptr<Overlay> Overlay::create(const Context& ctx, std::string* error) {
    if (!ctx.device || !ctx.renderer) {
        if (error) *error = "overlay needs a device and a renderer";
        return nullptr;
    }
    std::unique_ptr<Overlay> o(new Overlay());
    o->impl_ = new Impl();
    Impl& s = *o->impl_;
    s.ctx = ctx;
    s.color_format = ctx.renderer->format();
    if (ctx.renderer->output_kind() != extras::OutputKind::Sdr8) {
        // HDR swapchain: ImGui draws into an RGBA8 target that the composite maps onto the swapchain (see draw)
        if (s.composite.init(*ctx.device)) {
            s.hdr = true;
            s.color_format = VK_FORMAT_R8G8B8A8_UNORM;
        } else {
            NXLOG_W(kTag, "HDR overlay composite failed to initialise: the overlay is drawn straight into the HDR swapchain (colours will look wrong)");
        }
    }
    const std::string key = config_get_str("gfx", "overlay_key", "F1");
    if (const SDL_Scancode sc = SDL_GetScancodeFromName(key.c_str()); sc != SDL_SCANCODE_UNKNOWN) s.toggle_sc = sc;
    else NXLOG_W(kTag, "unknown [gfx] overlay_key '%s': using F1", key.c_str());

    IMGUI_CHECKVERSION();
    s.imgui = ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;     // no imgui.ini next to the exe
    io.LogFilename = nullptr;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard | ImGuiConfigFlags_NavEnableGamepad;
    io.BackendFlags |= ImGuiBackendFlags_HasGamepad;   // without it ImGui ignores the gamepad keys that set_keyboard_pad sends while the page is open
    ImGui::StyleColorsDark();
    add_fonts(io);
    ImGui::GetStyle().FontSizeBase = 16.0f;
    s.base_style = ImGui::GetStyle();
    const VkExtent2D e = ctx.renderer->extent();
    io.DisplaySize = ImVec2(static_cast<float>(e.width), static_cast<float>(e.height));

    VkPipelineRenderingCreateInfoKHR rci = {VK_STRUCTURE_TYPE_PIPELINE_RENDERING_CREATE_INFO_KHR};
    rci.colorAttachmentCount = 1;
    rci.pColorAttachmentFormats = &s.color_format;
    ImGui_ImplVulkan_InitInfo ii = {};
    ii.ApiVersion = VK_API_VERSION_1_3;
    ii.Instance = ctx.device->instance();
    ii.PhysicalDevice = ctx.device->physical();
    ii.Device = ctx.device->device();
    ii.QueueFamily = ctx.device->queue_family();
    ii.Queue = ctx.device->queue();
    ii.DescriptorPoolSize = 16;
    ii.MinImageCount = 2;
    ii.ImageCount = std::max(2u, ctx.renderer->image_count());
    ii.UseDynamicRendering = true;
    ii.PipelineInfoMain.PipelineRenderingCreateInfo = rci;
    ii.CheckVkResultFn = [](VkResult r) { if (r != VK_SUCCESS) NXLOG_E(kTag, "ImGui Vulkan call failed (%d)", static_cast<int>(r)); };
    if (!ImGui_ImplVulkan_Init(&ii)) {
        if (error) *error = "ImGui_ImplVulkan_Init failed";
        ImGui::DestroyContext(s.imgui);
        s.imgui = nullptr;
        return nullptr;
    }
    s.backend_ready = true;
    NXLOG_I(kTag, "overlay ready (toggle key %s)", SDL_GetScancodeName(static_cast<SDL_Scancode>(s.toggle_sc)));
    return o;
}

Overlay::~Overlay() {
    if (!impl_) return;
    if (impl_->ctx.device) impl_->ctx.device->wait_idle();
    impl_->composite.shutdown();
    if (impl_->backend_ready) ImGui_ImplVulkan_Shutdown();
    if (impl_->imgui) impl_->free_notice_textures();
    if (impl_->imgui) ImGui::DestroyContext(impl_->imgui);
    delete impl_;
}

void Overlay::set_visible(bool v) {
    Impl& s = *impl_;
    if (s.visible == v) return;
    s.visible = v;
    if (v) {
        s.restore_relative = platform::has_window() && platform::relative_mouse();
        if (s.restore_relative) platform::set_relative_mouse(false);
    } else {
        if (s.restore_relative && platform::has_window()) platform::set_relative_mouse(true);
        s.restore_relative = false;
        std::lock_guard<std::mutex> lk(s.mu);
        ImGuiContext* prev = ImGui::GetCurrentContext();
        ImGui::SetCurrentContext(s.imgui);
        ImGuiIO& io = ImGui::GetIO();   // drop pressed keys and buttons so nothing stays latched
        io.ClearInputKeys();
        io.ClearInputMouse();
        ImGui::SetCurrentContext(prev);
    }
    NXLOG_I(kTag, "overlay %s", v ? "shown" : "hidden");
}

bool Overlay::visible() const { return impl_->visible; }
void Overlay::toggle() { set_visible(!impl_->visible); }
int Overlay::toggle_scancode() const { return impl_->toggle_sc; }
void Overlay::set_toggle_scancode(int sc) { impl_->toggle_sc = sc; }
uint64_t Overlay::frames_drawn() const { return impl_->drawn; }

namespace {
bool imgui_wants(std::mutex& mu, ImGuiContext* ctx, bool mouse, bool keyboard) {
    std::lock_guard<std::mutex> lk(mu);
    ImGuiContext* prev = ImGui::GetCurrentContext();
    ImGui::SetCurrentContext(ctx);
    const ImGuiIO& io = ImGui::GetIO();
    const bool w = (mouse && io.WantCaptureMouse) || (keyboard && io.WantCaptureKeyboard);
    ImGui::SetCurrentContext(prev);
    return w;
}
}  // namespace

bool Overlay::wants_input() const { return impl_->visible && imgui_wants(impl_->mu, impl_->imgui, true, true); }
bool Overlay::wants_mouse() const { return impl_->visible && imgui_wants(impl_->mu, impl_->imgui, true, false); }
bool Overlay::wants_keyboard() const { return impl_->visible && imgui_wants(impl_->mu, impl_->imgui, false, true); }

void Overlay::add_panel(const std::string& title, std::function<void()> fn) { impl_->panels.push_back({title, std::move(fn)}); }

bool Overlay::handle_event(const input::Event& e) {
    Impl& s = *impl_;
    using input::EventType;
    if ((e.type == EventType::KeyDown || e.type == EventType::KeyUp) && e.u.key.scancode == s.toggle_sc) {
        if (e.type == EventType::KeyDown && !(e.flags & input::kFlagRepeat)) toggle();
        return true;   // the toggle key never reaches the game (press, repeat and release)
    }
    if (s.quit_open) {               // the quit prompt takes every key; the mouse goes to ImGui (its buttons), not to the game
        if (e.type == EventType::KeyDown || e.type == EventType::KeyUp || e.type == EventType::Text) return s.quit_key_event(e);
    }
    const bool kb = s.kb_poll() || s.quit_open;
    if (kb && !s.quit_open && (e.type == EventType::KeyDown || e.type == EventType::KeyUp || e.type == EventType::Text)) return s.kb_key_event(e);
    if (!s.visible && !kb) return false;
    std::lock_guard<std::mutex> lk(s.mu);
    ImGuiContext* prev = ImGui::GetCurrentContext();
    ImGui::SetCurrentContext(s.imgui);
    ImGuiIO& io = ImGui::GetIO();
    bool consumed = false;
    switch (e.type) {
        case EventType::KeyDown:
        case EventType::KeyUp: {
            const ImGuiKey k = map_key(e.u.key.scancode);
            if (k != ImGuiKey_None) io.AddKeyEvent(k, e.type == EventType::KeyDown);
            consumed = io.WantCaptureKeyboard;
            break;
        }
        case EventType::Text:
            io.AddInputCharactersUTF8(e.u.text.utf8);
            consumed = io.WantTextInput;
            break;
        case EventType::MouseMotion:
            io.AddMousePosEvent(e.u.motion.x, e.u.motion.y);
            consumed = io.WantCaptureMouse;
            break;
        case EventType::MouseButtonDown:
        case EventType::MouseButtonUp: {
            const int b = e.u.button.button;   // SDL: 1 left, 2 middle, 3 right
            const int ib = b == 1 ? 0 : b == 3 ? 1 : b == 2 ? 2 : b - 1;
            io.AddMousePosEvent(e.u.button.x, e.u.button.y);
            io.AddMouseButtonEvent(ib, e.type == EventType::MouseButtonDown);
            consumed = io.WantCaptureMouse;
            break;
        }
        case EventType::MouseWheel:
            io.AddMouseWheelEvent(e.u.wheel.dx, e.u.wheel.dy);
            consumed = io.WantCaptureMouse;
            break;
        case EventType::WindowFocusLost: io.AddFocusEvent(false); break;
        case EventType::WindowFocusGained: io.AddFocusEvent(true); break;
        default: break;
    }
    ImGui::SetCurrentContext(prev);
    // the on-screen keyboard and the quit prompt take the whole mouse while they are up
    if (kb) consumed = e.type == EventType::MouseMotion || e.type == EventType::MouseButtonDown || e.type == EventType::MouseButtonUp || e.type == EventType::MouseWheel;
    return consumed;
}

void Overlay::apply_pending() {
    apply_present_pending();
    apply_window_pending();
}

void Overlay::apply_present_pending() {
    Impl& s = *impl_;
    if (s.want_mode) {
        s.want_mode = false;
        s.ctx.renderer->set_present_mode(s.new_mode);
    }
}

void Overlay::apply_window_pending() {
    Impl& s = *impl_;
    if (s.want_fullscreen) {
        s.want_fullscreen = false;
        platform::set_fullscreen(s.new_fullscreen);
    }
}

void Overlay::draw(const FrameInfo& f) {
    Impl& s = *impl_;
    const bool counter_on = s.counter_show.load(std::memory_order_relaxed);
    const auto now = std::chrono::steady_clock::now();
    const double now_s = std::chrono::duration<double>(now.time_since_epoch()).count();
    const bool note_on = s.note_visible(now_s) && (s.counter_in_shots.load(std::memory_order_relaxed) || !s.ctx.renderer->capture_pending());
    const bool ptr_on = s.ptr_show.load(std::memory_order_relaxed) && (s.ptr_in_shots.load(std::memory_order_relaxed) || !s.ctx.renderer->capture_pending());
    const bool bare = s.ctx.renderer->capture_bare();   // a mod's screenshot: none of our menus, prompts or messages in it
    const bool menu_vis = s.visible && !bare;
    const bool quit_on = s.quit_open && !bare;
    const bool kb_on = !s.quit_open && s.kb_poll() && !bare;
    const bool save_on = s.save_notice_visible(now_s);
    const bool notices_on = notices::any() && !s.ctx.renderer->capture_pending();   // mods' messages: never in a screenshot
    const bool notice_tex_on = !s.notice_tex.empty();   // pictures of messages still held: frames go on until they are given back
    const bool layer_on = s.layer_wanted();   // the game's layer (overlay_layer.cpp)
    if (!menu_vis && !counter_on && !note_on && !save_on && !notices_on && !notice_tex_on && !ptr_on && !kb_on && !quit_on && !layer_on) {
        s.last_frame = now;
        s.counter_was_on = false;
        return;
    }
    if (counter_on) {
        if (!s.counter_was_on) {          // just switched on: start from an empty history
            s.counter_was_on = true;
            s.counter_have_prev = false;
            s.stats.reset();
            s.text_s = -1e9;
        }
        if (s.counter_have_prev) {
            const double dt_ms = std::chrono::duration<double, std::milli>(now - s.counter_prev).count();
            if (dt_ms <= 1000.0) s.stats.push(now_s, dt_ms);   // over a second: a pause (debugger, window in the background), not a frame
        }
        s.counter_prev = now;
        s.counter_have_prev = true;
    } else {
        s.counter_was_on = false;
    }
    // a screenshot leaves the counter out unless asked for; with the menu hidden there is then nothing to draw
    const bool show_counter = counter_on && (s.counter_in_shots.load(std::memory_order_relaxed) || !s.ctx.renderer->capture_pending());
    if (!menu_vis && !show_counter && !note_on && !save_on && !notices_on && !notice_tex_on && !ptr_on && !kb_on && !quit_on && !layer_on) {
        s.last_frame = now;
        return;
    }
    const bool hdr_frame = s.hdr && f.hdr_output;
    if (s.hdr && !f.hdr_output) return;      // the display left HDR mode: the RGBA8 overlay pipeline does not match the SDR swapchain, so no overlay until the next start
    if (hdr_frame && !s.composite.ensure_target(f.extent)) return;
    std::lock_guard<std::mutex> lk(s.mu);
    ImGuiContext* prev = ImGui::GetCurrentContext();
    ImGui::SetCurrentContext(s.imgui);
    ImGuiIO& io = ImGui::GetIO();
    io.DeltaTime = std::clamp(std::chrono::duration<float>(now - s.last_frame).count(), 1.0f / 1000.f, 0.25f);
    s.last_frame = now;
    io.DisplaySize = ImVec2(static_cast<float>(f.extent.width), static_cast<float>(f.extent.height));

    s.update_scale(f.extent);
    ImGui_ImplVulkan_NewFrame();
    ImGui::NewFrame();
    // the messages up now; during a screenshot none are read and their pictures are kept, otherwise every picture not among them goes
    std::vector<notices::Notice> notice_list;
    if (notices_on) notice_list = notices::shown(now_s);
    s.retire_notice_textures(notices_on || !notices::any() ? &notice_list : nullptr);
    if (layer_on) s.draw_game_layer(f);   // the background list: under everything below
    // the keyboard and the quit prompt are modal: the menu (and a keyboard under the prompt) comes back once they are answered
    if (menu_vis && !kb_on && !quit_on) s.build_ui();
    if (kb_on) s.draw_keyboard(now_s);
    if (quit_on) s.draw_quit_prompt();
    if (show_counter) s.draw_counter(f, now_s);
    if (note_on) s.draw_note(f);
    if (save_on) s.draw_save_notice(f);
    if (notices_on) s.draw_notices(f, notice_list);
    if (ptr_on) {   // a plain arrow, white with a dark outline, sized with the frame height
        const float k = std::max(1.f, static_cast<float>(f.extent.height) / 720.f);
        const float x = s.ptr_x.load(std::memory_order_relaxed), y = s.ptr_y.load(std::memory_order_relaxed);
        const ImVec2 pts[7] = {{0, 0}, {0, 16 * k}, {4 * k, 12.5f * k}, {7.5f * k, 19 * k}, {10 * k, 18 * k}, {6.5f * k, 11.5f * k}, {12 * k, 11.5f * k}};
        ImVec2 poly[7];
        for (int i = 0; i < 7; ++i) poly[i] = ImVec2(x + pts[i].x, y + pts[i].y);
        ImDrawList* dl = ImGui::GetForegroundDrawList();
        dl->AddConvexPolyFilled(poly, 7, IM_COL32(255, 255, 255, 255));
        dl->AddPolyline(poly, 7, IM_COL32(0, 0, 0, 255), ImDrawFlags_Closed, 1.5f * k);
    }
    ImGui::Render();

    // one dynamic-rendering pass that draws the ImGui draw data into view
    auto render_into = [&](VkImageView view, VkAttachmentLoadOp load) {
        VkRenderingAttachmentInfo att = {VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO};
        att.imageView = view;
        att.imageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        att.loadOp = load;
        att.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        att.clearValue.color = {{0.0f, 0.0f, 0.0f, 0.0f}};
        VkRenderingInfo ri = {VK_STRUCTURE_TYPE_RENDERING_INFO};
        ri.renderArea = {{0, 0}, f.extent};
        ri.layerCount = 1;
        ri.colorAttachmentCount = 1;
        ri.pColorAttachments = &att;
        vkCmdBeginRendering(f.cmd, &ri);
        ImGui_ImplVulkan_RenderDrawData(ImGui::GetDrawData(), f.cmd);
        vkCmdEndRendering(f.cmd);
    };
    if (hdr_frame) {
        // HDR: the overlay goes into the cleared RGBA8 target, then the composite maps it onto the swapchain image (UI white = paper white)
        const extras::HdrState hs = extras::hdr_state();
        s.composite.begin_overlay(f.cmd);
        render_into(s.composite.target_view(), VK_ATTACHMENT_LOAD_OP_CLEAR);
        s.composite.composite(f.cmd, f.view, f.format, f.extent, s.ctx.renderer->output_kind(), hs.paper_white_nits, hs.peak_nits);
    } else {
        render_into(f.view, VK_ATTACHMENT_LOAD_OP_LOAD);
    }
    s.drawn++;
    ImGui::SetCurrentContext(prev);
}

void Overlay::install_on_renderer() {
    impl_->ctx.renderer->set_overlay_hook([this](const FrameInfo& f) { draw(f); });
}

}  // namespace nxrt::gfx
