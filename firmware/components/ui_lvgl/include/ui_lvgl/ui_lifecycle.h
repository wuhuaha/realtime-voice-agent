#pragma once

namespace rva::ui {

// esp_lvgl_port owns a process-wide task and allocator. The current firmware
// initializes it once and tears it down only during final object destruction;
// an in-process Stop -> Start cycle is deliberately rejected.
class UiLifecycle final {
public:
    bool Begin() noexcept {
        if (active_) return true;
        if (consumed_) return false;
        consumed_ = true;
        active_ = true;
        return true;
    }

    void End() noexcept { active_ = false; }
    bool active() const noexcept { return active_; }
    bool consumed() const noexcept { return consumed_; }

private:
    bool consumed_ = false;
    bool active_ = false;
};

}  // namespace rva::ui
