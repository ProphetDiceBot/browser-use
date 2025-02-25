"""
DrissionPage browser on steroids.
"""

import asyncio
import gc
import logging
from dataclasses import dataclass, field

from drission.page import DrissionPage, ChromeOptions, new_tab
from drission.utils import time_execution_async  # Assuming a similar utility exists

from browser_use.browser.context import BrowserContext, BrowserContextConfig

logger = logging.getLogger(__name__)

@dataclass
class BrowserConfig:
    """
    Configuration for the Browser.
    
    Default values:
        headless: True
            Whether to run browser in headless mode
        
        disable_security: True
            Disable browser security features
        
        extra_chromium_args: []
            Extra arguments to pass to the browser
        
        wss_url: None
            Connect to a browser instance via WebSocket
        
        cdp_url: None
            Connect to a browser instance via CDP
        
        chrome_instance_path: None
            Path to a Chrome instance to use to connect to your normal browser
            e.g. '/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome'
    """

    headless: bool = False
    disable_security: bool = True
    extra_chromium_args: list[str] = field(default_factory=list)
    chrome_instance_path: str | None = None
    wss_url: str | None = None
    cdp_url: str | None = None

    proxy: dict | None = field(default=None)
    new_context_config: BrowserContextConfig = field(default_factory=BrowserContextConfig)

    _force_keep_browser_alive: bool = False


class Browser:
    """
    DrissionPage browser on steroids.

    This is a persistent browser factory that can spawn multiple browser contexts.
    It is recommended to use only one instance of Browser per your application (RAM usage will grow otherwise).
    """

    def __init__(
        self,
        config: BrowserConfig = BrowserConfig(),
    ):
        logger.debug('Initializing new browser')
        self.config = config
        self.drission: DrissionPage | None = None

        self.disable_security_args = []
        if self.config.disable_security:
            self.disable_security_args = [
                '--disable-web-security',
                '--disable-site-isolation-trials',
                '--disable-features=IsolateOrigins,site-per-process',
            ]

    async def new_context(self, config: BrowserContextConfig = BrowserContextConfig()) -> BrowserContext:
        """Create a browser context"""
        return BrowserContext(config=config, browser=self)

    async def get_drission_browser(self) -> DrissionPage:
        """Get a browser context"""
        if self.drission is None:
            return await self._init()

        return self.drission

    @time_execution_async('--init (browser)')
    async def _init(self):
        """Initialize the browser session"""
        options = ChromeOptions()
        if self.config.headless:
            options.add_argument('--headless')

        if self.config.extra_chromium_args:
            for arg in self.config.extra_chromium_args:
                options.add_argument(arg)

        if self.disable_security_args:
            for arg in self.disable_security_args:
                options.add_argument(arg)

        self.drission = DrissionPage(options=options)

        return self.drission

    async def _setup_cdp(self) -> DrissionPage:
        """Sets up and returns a DrissionPage instance with CDP."""
        if not self.config.cdp_url:
            raise ValueError('CDP URL is required')
        logger.info(f'Connecting to remote browser via CDP {self.config.cdp_url}')
        self.drission = DrissionPage(url=self.config.cdp_url)
        return self.drission

    async def _setup_wss(self) -> DrissionPage:
        """Sets up and returns a DrissionPage instance with WSS."""
        if not self.config.wss_url:
            raise ValueError('WSS URL is required')
        logger.info(f'Connecting to remote browser via WSS {self.config.wss_url}')
        self.drission = DrissionPage(url=self.config.wss_url)
        return self.drission

    async def _setup_browser_with_instance(self) -> DrissionPage:
        """Sets up and returns a DrissionPage instance with a Chrome instance."""
        if not self.config.chrome_instance_path:
            raise ValueError('Chrome instance path is required')
        import subprocess

        import requests

        try:
            # Check if browser is already running
            response = requests.get('http://localhost:9222/json/version', timeout=2)
            if response.status_code == 200:
                logger.info('Reusing existing Chrome instance')
                self.drission = DrissionPage(url='http://localhost:9222')
                return self.drission
        except requests.ConnectionError:
            logger.debug('No existing Chrome instance found, starting a new one')

        # Start a new Chrome instance
        subprocess.Popen(
            [
                self.config.chrome_instance_path,
                '--remote-debugging-port=9222',
            ]
            + self.config.extra_chromium_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Attempt to connect again after starting a new instance
        for _ in range(10):
            try:
                response = requests.get('http://localhost:9222/json/version', timeout=2)
                if response.status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            await asyncio.sleep(1)

        # Attempt to connect again after starting a new instance
        try:
            self.drission = DrissionPage(url='http://localhost:9222')
            return self.drission
        except Exception as e:
            logger.error(f'Failed to start a new Chrome instance.: {str(e)}')
            raise RuntimeError(
                'To start chrome in Debug mode, you need to close all existing Chrome instances and try again otherwise we cannot connect to the instance.'
            )

    async def _setup_standard_browser(self) -> DrissionPage:
        """Sets up and returns a DrissionPage instance with standard configurations."""
        options = ChromeOptions()
        if self.config.headless:
            options.add_argument('--headless')

        args = [
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
            '--disable-background-timer-throttling',
            '--disable-popup-blocking',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-window-activation',
            '--disable-focus-on-load',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-startup-window',
            '--window-position=0,0',
            # '--window-size=1280,1000',
        ]
        for arg in args + self.disable_security_args + self.config.extra_chromium_args:
            options.add_argument(arg)

        if self.config.proxy:
            options.add_argument(f'--proxy-server={self.config.proxy["server"]}')

        self.drission = DrissionPage(options=options)
        return self.drission

    async def _setup_browser(self) -> DrissionPage:
        """Sets up and returns a DrissionPage instance."""
        try:
            if self.config.cdp_url:
                return await self._setup_cdp()
            if self.config.wss_url:
                return await self._setup_wss()
            elif self.config.chrome_instance_path:
                return await self._setup_browser_with_instance()
            else:
                return await self._setup_standard_browser()
        except Exception as e:
            logger.error(f'Failed to initialize DrissionPage browser: {str(e)}')
            raise

    async def close(self):
        """Close the browser instance"""
        try:
            if not self.config._force_keep_browser_alive:
                if self.drission:
                    self.drission.close()
                    self.drission = None
        except Exception as e:
            logger.debug(f'Failed to close browser properly: {e}')
        finally:
            self.drission = None
            gc.collect()

    def __del__(self):
        """Cleanup when object is destroyed"""
        try:
            if self.drission:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self.close())
                else:
                    asyncio.run(self.close())
        except Exception as e:
            logger.debug(f'Failed to cleanup browser in destructor: {e}')
