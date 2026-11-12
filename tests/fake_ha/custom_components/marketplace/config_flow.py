import asyncio
import voluptuous as vol
from homeassistant import config_entries


class Flow(config_entries.ConfigFlow, domain="marketplace"):
    VERSION = 1
    task = None

    async def async_step_system(self, user_input=None):
        return self.async_create_entry(title="", data={})

    async def async_step_reconfigure(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({}))
        return await self.async_step_device()

    async def async_step_device(self, user_input=None):
        if self.task is None:
            self.task = self.hass.async_create_task(asyncio.sleep(6))
        if self.task.done():
            return self.async_show_progress_done(next_step_id="device_done")
        return self.async_show_progress(step_id="device", progress_action="wait_for_device",
                                        description_placeholders={"url": "https://github.com/login/device", "code": "WXYZ-9876"},
                                        progress_task=self.task)

    async def async_step_device_done(self, user_input=None):
        return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates={"token": "gh-oauth"})
