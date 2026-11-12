import asyncio
import voluptuous as vol
from homeassistant import config_entries


class Flow(config_entries.ConfigFlow, domain="hacs"):
    VERSION = 1
    task = None

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input:
            if not all(user_input.values()):
                return self.async_show_form(step_id="user", errors={"base": "acc"}, data_schema=self._schema())
            return await self.async_step_device()
        return self.async_show_form(step_id="user", data_schema=self._schema())

    @staticmethod
    def _schema():
        return vol.Schema({vol.Required(k, default=False): bool for k in ("acc_logs", "acc_addons", "acc_untested", "acc_disable")})

    async def async_step_device(self, user_input=None):
        if self.task is None:
            self.task = self.hass.async_create_task(asyncio.sleep(6))
        if self.task.done():
            return self.async_show_progress_done(next_step_id="device_done")
        return self.async_show_progress(step_id="device", progress_action="wait_for_device",
                                        description_placeholders={"url": "https://github.com/login/device", "code": "ABCD-1234"},
                                        progress_task=self.task)

    async def async_step_device_done(self, user_input=None):
        return self.async_create_entry(title="", data={"token": "fake"})
