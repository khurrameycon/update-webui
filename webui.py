import pdb
import logging

from dotenv import load_dotenv

load_dotenv()
import os
import glob
import asyncio
import argparse
import os

logger = logging.getLogger(__name__)

import gradio as gr
import inspect
from functools import wraps

from browser_use.agent.service import Agent
from playwright.async_api import async_playwright
from browser_use.browser.browser import Browser, BrowserConfig
from browser_use.browser.context import (
    BrowserContextConfig,
    BrowserContextWindowSize,
)
from langchain_ollama import ChatOllama
from playwright.async_api import async_playwright
from src.utils.agent_state import AgentState

from src.utils import utils
from src.agent.custom_agent import CustomAgent
from src.browser.custom_browser import CustomBrowser
from src.agent.custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt
from src.browser.custom_context import BrowserContextConfig, CustomBrowserContext
from src.controller.custom_controller import CustomController
from gradio.themes import Citrus, Default, Glass, Monochrome, Ocean, Origin, Soft, Base
from src.utils.utils import update_model_dropdown, get_latest_files, capture_screenshot, MissingAPIKeyError
from src.utils import utils
from src.utils.utils import save_llm_preferences, load_llm_preferences, update_model_dropdown
# Global variables for persistence
_global_browser = None
_global_browser_context = None
_global_agent = None
_global_api_keys = {}

# Create the global agent state instance
_global_agent_state = AgentState()

# webui config
webui_config_manager = utils.ConfigManager()


def scan_and_register_components(blocks):
    """扫描一个 Blocks 对象并注册其中的所有交互式组件，但不包括按钮"""
    global webui_config_manager

    def traverse_blocks(block, prefix=""):
        registered = 0

        # 处理 Blocks 自身的组件
        if hasattr(block, "children"):
            for i, child in enumerate(block.children):
                if isinstance(child, gr.components.Component):
                    # 排除按钮 (Button) 组件
                    if getattr(child, "interactive", False) and not isinstance(child, gr.Button):
                        name = f"{prefix}component_{i}"
                        if hasattr(child, "label") and child.label:
                            # 使用标签作为名称的一部分
                            label = child.label
                            name = f"{prefix}{label}"
                        logger.debug(f"Registering component: {name}")
                        webui_config_manager.register_component(name, child)
                        registered += 1
                elif hasattr(child, "children"):
                    # 递归处理嵌套的 Blocks
                    new_prefix = f"{prefix}block_{i}_"
                    registered += traverse_blocks(child, new_prefix)

        return registered

    total = traverse_blocks(blocks)
    logger.info(f"Total registered components: {total}")


def save_current_config():
    return webui_config_manager.save_current_config()


def update_ui_from_config(config_file):
    return webui_config_manager.update_ui_from_config(config_file)


def resolve_sensitive_env_variables(text):
    """
    Replace environment variable placeholders ($SENSITIVE_*) with their values.
    Only replaces variables that start with SENSITIVE_.
    """
    if not text:
        return text

    import re

    # Find all $SENSITIVE_* patterns
    env_vars = re.findall(r'\$SENSITIVE_[A-Za-z0-9_]*', text)

    result = text
    for var in env_vars:
        # Remove the $ prefix to get the actual environment variable name
        env_name = var[1:]  # removes the $
        env_value = os.getenv(env_name)
        if env_value is not None:
            # Replace $SENSITIVE_VAR_NAME with its value
            result = result.replace(var, env_value)

    return result


async def stop_agent():
    """Request the agent to stop and update UI with enhanced feedback"""
    global _global_agent

    try:
        if _global_agent is not None:
            # Request stop
            _global_agent.stop()
        # Update UI immediately
        message = "Stop requested - the agent will halt at the next safe point"
        logger.info(f"🛑 {message}")

        # Return UI updates
        return (
            gr.update(value="Stopping...", interactive=False),  # stop_button
            gr.update(interactive=False),  # run_button
        )
    except Exception as e:
        error_msg = f"Error during stop: {str(e)}"
        logger.error(error_msg)
        return (
            gr.update(value="Stop", interactive=True),
            gr.update(interactive=True)
        )


async def stop_research_agent():
    """Request the agent to stop and update UI with enhanced feedback"""
    global _global_agent_state

    try:
        # Request stop
        _global_agent_state.request_stop()

        # Update UI immediately
        message = "Stop requested - the agent will halt at the next safe point"
        logger.info(f"🛑 {message}")

        # Return UI updates
        return (  # errors_output
            gr.update(value="Stopping...", interactive=False),  # stop_button
            gr.update(interactive=False),  # run_button
        )
    except Exception as e:
        error_msg = f"Error during stop: {str(e)}"
        logger.error(error_msg)
        return (
            gr.update(value="Stop", interactive=True),
            gr.update(interactive=True)
        )


async def run_browser_agent(
        agent_type,
        llm_provider,
        llm_model_name,
        llm_num_ctx,
        llm_temperature,
        llm_base_url,
        llm_api_key,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        enable_recording,
        task,
        add_infos,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method,
        chrome_cdp,
        max_input_tokens
):
    try:
        # Disable recording if the checkbox is unchecked
        if not enable_recording:
            save_recording_path = None

        # Ensure the recording directory exists if recording is enabled
        if save_recording_path:
            os.makedirs(save_recording_path, exist_ok=True)

        # Get the list of existing videos before the agent runs
        existing_videos = set()
        if save_recording_path:
            existing_videos = set(
                glob.glob(os.path.join(save_recording_path, "*.[mM][pP]4"))
                + glob.glob(os.path.join(save_recording_path, "*.[wW][eE][bB][mM]"))
            )

        task = resolve_sensitive_env_variables(task)

        # Run the agent
        llm = utils.get_llm_model(
            provider=llm_provider,
            model_name=llm_model_name,
            num_ctx=llm_num_ctx,
            temperature=llm_temperature,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )
        if agent_type == "org":
            final_result, errors, model_actions, model_thoughts, trace_file, history_file = await run_org_agent(
                llm=llm,
                use_own_browser=use_own_browser,
                keep_browser_open=keep_browser_open,
                headless=headless,
                disable_security=disable_security,
                window_w=window_w,
                window_h=window_h,
                save_recording_path=save_recording_path,
                save_agent_history_path=save_agent_history_path,
                save_trace_path=save_trace_path,
                task=task,
                max_steps=max_steps,
                use_vision=use_vision,
                max_actions_per_step=max_actions_per_step,
                tool_calling_method=tool_calling_method,
                chrome_cdp=chrome_cdp,
                max_input_tokens=max_input_tokens
            )
        elif agent_type == "custom":
            final_result, errors, model_actions, model_thoughts, trace_file, history_file = await run_custom_agent(
                llm=llm,
                use_own_browser=use_own_browser,
                keep_browser_open=keep_browser_open,
                headless=headless,
                disable_security=disable_security,
                window_w=window_w,
                window_h=window_h,
                save_recording_path=save_recording_path,
                save_agent_history_path=save_agent_history_path,
                save_trace_path=save_trace_path,
                task=task,
                add_infos=add_infos,
                max_steps=max_steps,
                use_vision=use_vision,
                max_actions_per_step=max_actions_per_step,
                tool_calling_method=tool_calling_method,
                chrome_cdp=chrome_cdp,
                max_input_tokens=max_input_tokens
            )
        else:
            raise ValueError(f"Invalid agent type: {agent_type}")

        # Get the list of videos after the agent runs (if recording is enabled)
        # latest_video = None
        # if save_recording_path:
        #     new_videos = set(
        #         glob.glob(os.path.join(save_recording_path, "*.[mM][pP]4"))
        #         + glob.glob(os.path.join(save_recording_path, "*.[wW][eE][bB][mM]"))
        #     )
        #     if new_videos - existing_videos:
        #         latest_video = list(new_videos - existing_videos)[0]  # Get the first new video

        gif_path = os.path.join(os.path.dirname(__file__), "agent_history.gif")

        return (
            final_result,
            errors,
            model_actions,
            model_thoughts,
            gif_path,
            trace_file,
            history_file,
            gr.update(value="Stop", interactive=True),  # Re-enable stop button
            gr.update(interactive=True)  # Re-enable run button
        )

    except MissingAPIKeyError as e:
        logger.error(str(e))
        raise gr.Error(str(e), print_exception=False)

    except Exception as e:
        import traceback
        traceback.print_exc()
        errors = str(e) + "\n" + traceback.format_exc()
        return (
            '',  # final_result
            errors,  # errors
            '',  # model_actions
            '',  # model_thoughts
            None,  # latest_video
            None,  # history_file
            None,  # trace_file
            gr.update(value="Stop", interactive=True),  # Re-enable stop button
            gr.update(interactive=True)  # Re-enable run button
        )


async def run_org_agent(
        llm,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        task,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method,
        chrome_cdp,
        max_input_tokens
):
    try:
        global _global_browser, _global_browser_context, _global_agent

        extra_chromium_args = ["--accept_downloads=True", f"--window-size={window_w},{window_h}"]
        cdp_url = chrome_cdp

        if use_own_browser:
            cdp_url = os.getenv("CHROME_CDP", chrome_cdp)
            chrome_path = os.getenv("CHROME_PATH", None)
            if chrome_path == "":
                chrome_path = None
            chrome_user_data = os.getenv("CHROME_USER_DATA", None)
            if chrome_user_data:
                extra_chromium_args += [f"--user-data-dir={chrome_user_data}"]
        else:
            chrome_path = None

        if _global_browser is None:
            _global_browser = Browser(
                config=BrowserConfig(
                    headless=headless,
                    cdp_url=cdp_url,
                    disable_security=disable_security,
                    chrome_instance_path=chrome_path,
                    extra_chromium_args=extra_chromium_args,
                )
            )

        if _global_browser_context is None:
            _global_browser_context = await _global_browser.new_context(
                config=BrowserContextConfig(
                    trace_path=save_trace_path if save_trace_path else None,
                    save_recording_path=save_recording_path if save_recording_path else None,
                    save_downloads_path="./tmp/downloads",
                    no_viewport=False,
                    browser_window_size=BrowserContextWindowSize(
                        width=window_w, height=window_h
                    ),
                )
            )

        if _global_agent is None:
            _global_agent = Agent(
                task=task,
                llm=llm,
                use_vision=use_vision,
                browser=_global_browser,
                browser_context=_global_browser_context,
                max_actions_per_step=max_actions_per_step,
                tool_calling_method=tool_calling_method,
                max_input_tokens=max_input_tokens,
                generate_gif=True
            )
        history = await _global_agent.run(max_steps=max_steps)

        history_file = os.path.join(save_agent_history_path, f"{_global_agent.state.agent_id}.json")
        _global_agent.save_history(history_file)

        final_result = history.final_result()
        errors = history.errors()
        model_actions = history.model_actions()
        model_thoughts = history.model_thoughts()

        trace_file = get_latest_files(save_trace_path)

        return final_result, errors, model_actions, model_thoughts, trace_file.get('.zip'), history_file
    except Exception as e:
        import traceback
        traceback.print_exc()
        errors = str(e) + "\n" + traceback.format_exc()
        return '', errors, '', '', None, None
    finally:
        _global_agent = None
        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            if _global_browser_context:
                await _global_browser_context.close()
                _global_browser_context = None

            if _global_browser:
                await _global_browser.close()
                _global_browser = None


async def run_custom_agent(
        llm,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        task,
        add_infos,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method,
        chrome_cdp,
        max_input_tokens
):
    try:
        global _global_browser, _global_browser_context, _global_agent

        extra_chromium_args = ["--accept_downloads=True", f"--window-size={window_w},{window_h}"]
        cdp_url = chrome_cdp
        if use_own_browser:
            cdp_url = os.getenv("CHROME_CDP", chrome_cdp)

            chrome_path = os.getenv("CHROME_PATH", None)
            if chrome_path == "":
                chrome_path = None
            chrome_user_data = os.getenv("CHROME_USER_DATA", None)
            if chrome_user_data:
                extra_chromium_args += [f"--user-data-dir={chrome_user_data}"]
        else:
            chrome_path = None

        controller = CustomController()

        # Initialize global browser if needed
        # if chrome_cdp not empty string nor None
        if (_global_browser is None) or (cdp_url and cdp_url != "" and cdp_url != None):
            _global_browser = CustomBrowser(
                config=BrowserConfig(
                    headless=headless,
                    disable_security=disable_security,
                    cdp_url=cdp_url,
                    chrome_instance_path=chrome_path,
                    extra_chromium_args=extra_chromium_args,
                )
            )

        if _global_browser_context is None or (chrome_cdp and cdp_url != "" and cdp_url != None):
            _global_browser_context = await _global_browser.new_context(
                config=BrowserContextConfig(
                    trace_path=save_trace_path if save_trace_path else None,
                    save_recording_path=save_recording_path if save_recording_path else None,
                    no_viewport=False,
                    save_downloads_path="./tmp/downloads",
                    browser_window_size=BrowserContextWindowSize(
                        width=window_w, height=window_h
                    ),
                )
            )

        # Create and run agent
        if _global_agent is None:
            _global_agent = CustomAgent(
                task=task,
                add_infos=add_infos,
                use_vision=use_vision,
                llm=llm,
                browser=_global_browser,
                browser_context=_global_browser_context,
                controller=controller,
                system_prompt_class=CustomSystemPrompt,
                agent_prompt_class=CustomAgentMessagePrompt,
                max_actions_per_step=max_actions_per_step,
                tool_calling_method=tool_calling_method,
                max_input_tokens=max_input_tokens,
                generate_gif=True
            )
        history = await _global_agent.run(max_steps=max_steps)

        history_file = os.path.join(save_agent_history_path, f"{_global_agent.state.agent_id}.json")
        _global_agent.save_history(history_file)

        final_result = history.final_result()
        errors = history.errors()
        model_actions = history.model_actions()
        model_thoughts = history.model_thoughts()

        trace_file = get_latest_files(save_trace_path)

        return final_result, errors, model_actions, model_thoughts, trace_file.get('.zip'), history_file
    except Exception as e:
        import traceback
        traceback.print_exc()
        errors = str(e) + "\n" + traceback.format_exc()
        return '', errors, '', '', None, None
    finally:
        _global_agent = None
        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            if _global_browser_context:
                await _global_browser_context.close()
                _global_browser_context = None

            if _global_browser:
                await _global_browser.close()
                _global_browser = None


async def run_with_stream(
        agent_type,
        llm_provider,
        llm_model_name,
        llm_num_ctx,
        llm_temperature,
        llm_base_url,
        llm_api_key,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        enable_recording,
        task,
        add_infos,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method,
        chrome_cdp,
        max_input_tokens
):
    global _global_agent

    stream_vw = 80
    stream_vh = int(80 * window_h // window_w)
    if not headless:
        result = await run_browser_agent(
            agent_type=agent_type,
            llm_provider=llm_provider,
            llm_model_name=llm_model_name,
            llm_num_ctx=llm_num_ctx,
            llm_temperature=llm_temperature,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            use_own_browser=use_own_browser,
            keep_browser_open=keep_browser_open,
            headless=headless,
            disable_security=disable_security,
            window_w=window_w,
            window_h=window_h,
            save_recording_path=save_recording_path,
            save_agent_history_path=save_agent_history_path,
            save_trace_path=save_trace_path,
            enable_recording=enable_recording,
            task=task,
            add_infos=add_infos,
            max_steps=max_steps,
            use_vision=use_vision,
            max_actions_per_step=max_actions_per_step,
            tool_calling_method=tool_calling_method,
            chrome_cdp=chrome_cdp,
            max_input_tokens=max_input_tokens
        )
        # Add HTML content at the start of the result array
        yield [gr.update(visible=False)] + list(result)
    else:
        try:
            # Run the browser agent in the background
            agent_task = asyncio.create_task(
                run_browser_agent(
                    agent_type=agent_type,
                    llm_provider=llm_provider,
                    llm_model_name=llm_model_name,
                    llm_num_ctx=llm_num_ctx,
                    llm_temperature=llm_temperature,
                    llm_base_url=llm_base_url,
                    llm_api_key=llm_api_key,
                    use_own_browser=use_own_browser,
                    keep_browser_open=keep_browser_open,
                    headless=headless,
                    disable_security=disable_security,
                    window_w=window_w,
                    window_h=window_h,
                    save_recording_path=save_recording_path,
                    save_agent_history_path=save_agent_history_path,
                    save_trace_path=save_trace_path,
                    enable_recording=enable_recording,
                    task=task,
                    add_infos=add_infos,
                    max_steps=max_steps,
                    use_vision=use_vision,
                    max_actions_per_step=max_actions_per_step,
                    tool_calling_method=tool_calling_method,
                    chrome_cdp=chrome_cdp,
                    max_input_tokens=max_input_tokens
                )
            )

            # Initialize values for streaming
            html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Using browser...</h1>"
            final_result = errors = model_actions = model_thoughts = ""
            recording_gif = trace = history_file = None

            # Periodically update the stream while the agent task is running
            while not agent_task.done():
                try:
                    encoded_screenshot = await capture_screenshot(_global_browser_context)
                    if encoded_screenshot is not None:
                        html_content = f'<img src="data:image/jpeg;base64,{encoded_screenshot}" style="width:{stream_vw}vw; height:{stream_vh}vh ; border:1px solid #ccc;">'
                    else:
                        html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>"
                except Exception as e:
                    html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>"

                if _global_agent and _global_agent.state.stopped:
                    yield [
                        gr.HTML(value=html_content, visible=True),
                        final_result,
                        errors,
                        model_actions,
                        model_thoughts,
                        recording_gif,
                        trace,
                        history_file,
                        gr.update(value="Stopping...", interactive=False),  # stop_button
                        gr.update(interactive=False),  # run_button
                    ]
                    break
                else:
                    yield [
                        gr.HTML(value=html_content, visible=True),
                        final_result,
                        errors,
                        model_actions,
                        model_thoughts,
                        recording_gif,
                        trace,
                        history_file,
                        gr.update(),  # Re-enable stop button
                        gr.update()  # Re-enable run button
                    ]
                await asyncio.sleep(0.1)

            # Once the agent task completes, get the results
            try:
                result = await agent_task
                final_result, errors, model_actions, model_thoughts, recording_gif, trace, history_file, stop_button, run_button = result
            except gr.Error:
                final_result = ""
                model_actions = ""
                model_thoughts = ""
                recording_gif = trace = history_file = None

            except Exception as e:
                errors = f"Agent error: {str(e)}"

            yield [
                gr.HTML(value=html_content, visible=True),
                final_result,
                errors,
                model_actions,
                model_thoughts,
                recording_gif,
                trace,
                history_file,
                stop_button,
                run_button
            ]

        except Exception as e:
            import traceback
            yield [
                gr.HTML(
                    value=f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>",
                    visible=True),
                "",
                f"Error: {str(e)}\n{traceback.format_exc()}",
                "",
                "",
                None,
                None,
                None,
                gr.update(value="Stop", interactive=True),  # Re-enable stop button
                gr.update(interactive=True)  # Re-enable run button
            ]


# Define the theme map globally
theme_map = {
    "Default": Default(),
    "Soft": Soft(),
    "Monochrome": Monochrome(),
    "Glass": Glass(),
    "Origin": Origin(),
    "Citrus": Citrus(),
    "Ocean": Ocean(),
    "Base": Base()
}


async def close_global_browser():
    global _global_browser, _global_browser_context

    if _global_browser_context:
        await _global_browser_context.close()
        _global_browser_context = None

    if _global_browser:
        await _global_browser.close()
        _global_browser = None


async def run_deep_search(research_task, max_search_iteration_input, max_query_per_iter_input, llm_provider,
                          llm_model_name, llm_num_ctx, llm_temperature, llm_base_url, llm_api_key, use_vision,
                          use_own_browser, headless, chrome_cdp):
    from src.utils.deep_research import deep_research
    global _global_agent_state

    # Clear any previous stop request
    _global_agent_state.clear_stop()

    llm = utils.get_llm_model(
        provider=llm_provider,
        model_name=llm_model_name,
        num_ctx=llm_num_ctx,
        temperature=llm_temperature,
        base_url=llm_base_url,
        api_key=llm_api_key,
    )
    markdown_content, file_path = await deep_research(research_task, llm, _global_agent_state,
                                                      max_search_iterations=max_search_iteration_input,
                                                      max_query_num=max_query_per_iter_input,
                                                      use_vision=use_vision,
                                                      headless=headless,
                                                      use_own_browser=use_own_browser,
                                                      chrome_cdp=chrome_cdp
                                                      )

    return markdown_content, file_path, gr.update(value="Stop", interactive=True), gr.update(interactive=True)


def handle_research_chat(message, chat_history, research_content, llm_provider, llm_model_name, llm_num_ctx, llm_temperature, llm_base_url, llm_api_key):
    """Handle chat about research results"""
    
    if not message.strip():
        return chat_history, ""
    
    if not research_content or research_content == "":
        return chat_history + [["User", message], ["Assistant", "Please run a research task first before asking questions."]], ""
    
    try:
        # Get LLM
        llm = utils.get_llm_model(
            provider=llm_provider,
            model_name=llm_model_name,
            num_ctx=llm_num_ctx,
            temperature=llm_temperature,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )
        
        # Create system prompt
        system_prompt = f"""You are a helpful research assistant. A user has conducted research and received the following research report:

---RESEARCH REPORT---
{research_content}
---END RESEARCH REPORT---

The user wants to discuss this research or ask for modifications. Please respond helpfully based on the research content. If they ask for changes or additions, provide the modified content in markdown format.

Be conversational and helpful."""

        # Prepare messages for LLM
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        
        messages = [SystemMessage(content=system_prompt)]
        
        # Add chat history
        if chat_history:
            for chat_msg in chat_history:
                messages.append(HumanMessage(content=chat_msg[0]))
                messages.append(AIMessage(content=chat_msg[1]))
        
        # Add current message
        messages.append(HumanMessage(content=message))
        
        # Get LLM response
        response = llm.invoke(messages)
        ai_response = response.content
        
        # Update chat history
        if chat_history is None:
            chat_history = []
        
        chat_history.append([message, ai_response])
        
        return chat_history, ""
        
    except Exception as e:
        if chat_history is None:
            chat_history = []
        chat_history.append([message, f"Sorry, I encountered an error: {str(e)}"])
        return chat_history, ""

hide_menu_css = """
    /* Hide Recordings and UI Configuration menu items */
    div[class*="svelte-"][class*="py-1"] > button[role="menuitem"]:nth-of-type(1),
    div[class*="svelte-"][class*="py-1"] > button[role="menuitem"]:nth-of-type(2)
    {
        display: none !important;
    }
    """ 
def create_ui(theme_name="Ocean"):
    css = """
    .gradio-container {
        width: 60vw !important; 
        max-width: 60% !important; 
        margin-left: auto !important;
        margin-right: auto !important;
        padding-top: 20px !important;
    }
    .header-text {
        text-align: center;
        margin-bottom: 30px;
    }
    .theme-section {
        margin-bottom: 20px;
        padding: 15px;
        border-radius: 10px;
    }
    div.overflow-dropdown > button:nth-of-type(1),
    div.overflow-dropdown > button:nth-of-type(2)
    {
        display: none !important;
    }
    """
    global _global_api_keys
    saved_prefs = load_llm_preferences()
    default_provider, default_model, loaded_api_keys = load_llm_preferences()
    _global_api_keys = loaded_api_keys # Store loaded keys globally
    default_api_key = _global_api_keys.get(default_provider, "") # Get key for the default provider
    # --- End updated loading logic ---

    # Dynamically get choices for the default model
    default_model_choices = utils.model_names.get(default_provider, [])
    # Ensure the loaded default model is valid for the provider
    if default_model not in default_model_choices:
        # If saved model is invalid for the loaded provider, pick the first valid one
        default_model = default_model_choices[0] if default_model_choices else ""
        # Alternatively, if allow_custom_value=True, could keep the saved name:
        # default_model = default_model

    with gr.Blocks(
            title="Browser Use WebUI", theme=theme_map[theme_name], css=css
    ) as demo:
        
        with gr.Tabs(selected=2) as tabs:
            with gr.TabItem("⚙️ Agent Settings", id=1, visible=False):
                with gr.Group():
                    agent_type = gr.Radio(
                        ["org", "custom"],
                        label="Agent Type",
                        value="custom",
                        info="Select the type of agent to use",
                        interactive=True
                    )
                    with gr.Column():
                        max_steps = gr.Slider(
                            minimum=1,
                            maximum=200,
                            value=100,
                            step=1,
                            label="Max Run Steps",
                            info="Maximum number of steps the agent will take",
                            interactive=True
                        )
                        max_actions_per_step = gr.Slider(
                            minimum=1,
                            maximum=100,
                            value=10,
                            step=1,
                            label="Max Actions per Step",
                            info="Maximum number of actions the agent will take per step",
                            interactive=True
                        )
                    with gr.Column():
                        use_vision = gr.Checkbox(
                            label="Use Vision",
                            value=True,
                            info="Enable visual processing capabilities",
                            interactive=True
                        )
                        max_input_tokens = gr.Number(
                            label="Max Input Tokens",
                            value=128000,
                            precision=0,
                            interactive=True
                        )
                        tool_calling_method = gr.Dropdown(
                            label="Tool Calling Method",
                            value="auto",
                            interactive=True,
                            allow_custom_value=True,  # Allow users to input custom model names
                            choices=["auto", "json_schema", "function_calling"],
                            info="Tool Calls Funtion Name",
                            visible=False
                        )

            with gr.TabItem("🔧 LLM Settings", id=2):
                with gr.Group():
                    llm_provider = gr.Dropdown(
                        choices=[provider for provider, model in utils.model_names.items()],
                        label="LLM Provider",
                        value="google",
                        info="Select your preferred language model provider",
                        interactive=True
                    )
                    llm_model_name = gr.Dropdown(
                        label="Model Name",
                        choices=utils.model_names['google'],
                        value="gemini-2.0-flash",
                        interactive=True,
                        allow_custom_value=True,  # Allow users to input custom model names
                        info="Select a model in the dropdown options or directly type a custom model name"
                    )
                    ollama_num_ctx = gr.Slider(
                        minimum=2 ** 8,
                        maximum=2 ** 16,
                        value=16000,
                        step=1,
                        label="Ollama Context Length",
                        info="Controls max context length model needs to handle (less = faster)",
                        visible=False,
                        interactive=True
                    )
                    llm_temperature = gr.Slider(
                        minimum=0.0,
                        maximum=2.0,
                        value=0.6,
                        step=0.1,
                        label="Temperature",
                        info="Controls randomness in model outputs",
                        interactive=True
                    )
                    with gr.Row():
                        llm_base_url = gr.Textbox(
                            label="Base URL",
                            value="",
                            visible = False,
                            info="API endpoint URL (if required)"
                        )
                        llm_api_key = gr.Textbox(
                            label="API Key",
                            type="password",
                            value="",
                            info="Your API key (leave blank to use .env)"
                        )
                    with gr.Row():
                        save_next_button = gr.Button("Save and Next", variant="secondary")
                        llm_save_status = gr.Textbox(label="Status", interactive=False, visible=False)



            # Change event to update context length slider
            def update_llm_num_ctx_visibility(llm_provider):
                return gr.update(visible=llm_provider == "ollama")

            # Bind the change event of llm_provider to update the visibility of context length slider
            def provider_changed(selected_provider):
                global _global_api_keys
                # 1. Update model dropdown
                model_update = update_model_dropdown(selected_provider) # This returns a gr.Dropdown update
                # 2. Update API key field
                api_key_update = _global_api_keys.get(selected_provider, "")
                # 3. Update Ollama slider visibility
                ollama_visible = (selected_provider == "ollama")
                # Return updates for all three components
                return [
                    model_update, # Update for llm_model_name
                    gr.Textbox(value=api_key_update), # Update for llm_api_key
                    gr.Slider(visible=ollama_visible) # Update for ollama_num_ctx
                ]

            # --- Bind provider change event ---
            llm_provider.change(
                fn=provider_changed,
                inputs=llm_provider,
                # Outputs need to match the return order in provider_changed
                outputs=[llm_model_name, llm_api_key, ollama_num_ctx]
            )

            with gr.TabItem("🌐 Browser Settings", id=3, visible=False):
                with gr.Group():
                    with gr.Row():
                        use_own_browser = gr.Checkbox(
                            label="Use Own Browser",
                            value=False,
                            info="Use your existing browser instance",
                            interactive=True
                        )
                        keep_browser_open = gr.Checkbox(
                            label="Keep Browser Open",
                            value=False,
                            info="Keep Browser Open between Tasks",
                            interactive=True
                        )
                        headless = gr.Checkbox(
                            label="Headless Mode",
                            value=False,
                            info="Run browser without GUI",
                            interactive=True
                        )
                        disable_security = gr.Checkbox(
                            label="Disable Security",
                            value=True,
                            info="Disable browser security features",
                            interactive=True
                        )
                        enable_recording = gr.Checkbox(
                            label="Enable Recording",
                            value=True,
                            info="Enable saving browser recordings",
                            interactive=True
                        )

                    with gr.Row():
                        window_w = gr.Number(
                            label="Window Width",
                            value=1280,
                            info="Browser window width",
                            interactive=True
                        )
                        window_h = gr.Number(
                            label="Window Height",
                            value=1100,
                            info="Browser window height",
                            interactive=True
                        )

                    chrome_cdp = gr.Textbox(
                        label="CDP URL",
                        placeholder="http://localhost:9222",
                        value="",
                        info="CDP for google remote debugging",
                        interactive=True,  # Allow editing only if recording is enabled
                    )

                    save_recording_path = gr.Textbox(
                        label="Recording Path",
                        placeholder="e.g. ./tmp/record_videos",
                        value="./tmp/record_videos",
                        info="Path to save browser recordings",
                        interactive=True,  # Allow editing only if recording is enabled
                    )

                    save_trace_path = gr.Textbox(
                        label="Trace Path",
                        placeholder="e.g. ./tmp/traces",
                        value="./tmp/traces",
                        info="Path to save Agent traces",
                        interactive=True,
                    )

                    save_agent_history_path = gr.Textbox(
                        label="Agent History Save Path",
                        placeholder="e.g., ./tmp/agent_history",
                        value="./tmp/agent_history",
                        info="Specify the directory where agent history should be saved.",
                        interactive=True,
                    )

            with gr.TabItem("Visual Web Agent", id=4):
                task = gr.Textbox(
                    label="Task Description",
                    lines=4,
                    placeholder="Enter your task here...",
                    value="go to google.com and type 'OpenAI' click search and give me the first url",
                    info="Describe what you want the agent to do",
                    interactive=True
                )
                add_infos = gr.Textbox(
                    label="Additional Information",
                    lines=3,
                    placeholder="Add any helpful context or instructions...",
                    info="Optional hints to help the LLM complete the task",
                    value="",
                    visible = False, 
                    interactive=True
                )

                with gr.Row():
                    run_button = gr.Button("▶️ Run Agent", variant="primary", scale=2)
                    stop_button = gr.Button("⏹️ Stop", variant="stop", scale=1)

                with gr.Row():
                    browser_view = gr.HTML(
                        value="<h1 style='width:80vw; height:50vh'>Waiting for browser session...</h1>",
                        label="Live Browser View",
                        visible=False
                    )

                gr.Markdown("### Results")
                with gr.Row():
                    with gr.Column():
                        final_result_output = gr.Textbox(
                            label="Final Result", lines=3, show_label=True
                        )
                    with gr.Column():
                        errors_output = gr.Textbox(
                            label="Errors", lines=3, show_label=True
                        )
                with gr.Row():
                    with gr.Column():
                        model_actions_output = gr.Textbox(
                            label="Model Actions", lines=3, show_label=True, visible=False
                        )
                    with gr.Column():
                        model_thoughts_output = gr.Textbox(
                            label="Model Thoughts", lines=3, show_label=True, visible=False
                        )
                recording_gif = gr.Image(label="Result GIF", format="gif", visible=False)
                trace_file = gr.File(label="Trace File", visible=False)
                agent_history_file = gr.File(label="Agent History", visible=False)

            with gr.TabItem("Research Agent", id=5):
                research_task_input = gr.Textbox(label="Research Task", lines=5,
                                                value="Compose a report on the use of Reinforcement Learning for training Large Language Models, encompassing its origins, current advancements, and future prospects, substantiated with examples of relevant models and techniques. The report should reflect original insights and analysis, moving beyond mere summarization of existing literature.",
                                                interactive=True)
                with gr.Row():
                    max_search_iteration_input = gr.Number(label="Max Search Iteration", value=3,
                                                        precision=0,
                                                        interactive=True)  # precision=0 确保是整数
                    max_query_per_iter_input = gr.Number(label="Max Query per Iteration", value=1,
                                                        precision=0,
                                                        interactive=True)  # precision=0 确保是整数
                with gr.Row():
                    research_button = gr.Button("▶️ Run Deep Research", variant="primary", scale=2)
                    stop_research_button = gr.Button("⏹ Stop", variant="stop", scale=1)
                
                markdown_output_display = gr.Markdown(label="Research Report")
                markdown_download = gr.File(label="Download Research Report")
                
                # ADD CHAT INTERFACE HERE
                gr.Markdown("### 💬 Chat about Research Results")
                research_chat_history = gr.Chatbot(label="Research Chat", height=300)
                with gr.Row():
                    research_chat_input = gr.Textbox(
                        label="Ask about the research", 
                        placeholder="e.g., 'Can you make this more detailed?' or 'Add information about recent developments'",
                        lines=2,
                        scale=4
                    )
                    research_chat_send = gr.Button("💬 Send", variant="primary", scale=1)

            # Bind the stop button click event after errors_output is defined
            stop_button.click(
                fn=stop_agent,
                inputs=[],
                outputs=[stop_button, run_button],
            )

            # Run button click handler
            run_button.click(
                fn=run_with_stream,
                inputs=[
                    agent_type, llm_provider, llm_model_name, ollama_num_ctx, llm_temperature, llm_base_url,
                    llm_api_key,
                    use_own_browser, keep_browser_open, headless, disable_security, window_w, window_h,
                    save_recording_path, save_agent_history_path, save_trace_path,  # Include the new path
                    enable_recording, task, add_infos, max_steps, use_vision, max_actions_per_step,
                    tool_calling_method, chrome_cdp, max_input_tokens
                ],
                outputs=[
                    browser_view,  # Browser view
                    final_result_output,  # Final result
                    errors_output,  # Errors
                    model_actions_output,  # Model actions
                    model_thoughts_output,  # Model thoughts
                    recording_gif,  # Latest recording
                    trace_file,  # Trace file
                    agent_history_file,  # Agent history file
                    stop_button,  # Stop button
                    run_button  # Run button
                ],
            )

            # Run Deep Research
            research_button.click(
                fn=run_deep_search,
                inputs=[research_task_input, max_search_iteration_input, max_query_per_iter_input, llm_provider,
                        llm_model_name, ollama_num_ctx, llm_temperature, llm_base_url, llm_api_key, use_vision,
                        use_own_browser, headless, chrome_cdp],
                outputs=[markdown_output_display, markdown_download, stop_research_button, research_button]
            )
            # Bind the stop button click event after errors_output is defined
            stop_research_button.click(
                fn=stop_research_agent,
                inputs=[],
                outputs=[stop_research_button, research_button],
            )
            # ADD THIS CHAT HANDLER
            research_chat_send.click(
                fn=handle_research_chat,
                inputs=[research_chat_input, research_chat_history, markdown_output_display, llm_provider,
                        llm_model_name, ollama_num_ctx, llm_temperature, llm_base_url, llm_api_key],
                outputs=[research_chat_history, research_chat_input]
            )

            with gr.TabItem("🎥 Recordings", id=7, visible=False):
                def list_recordings(save_recording_path):
                    if not os.path.exists(save_recording_path):
                        return []

                    # Get all video files
                    recordings = glob.glob(os.path.join(save_recording_path, "*.[mM][pP]4")) + glob.glob(
                        os.path.join(save_recording_path, "*.[wW][eE][bB][mM]"))

                    # Sort recordings by creation time (oldest first)
                    recordings.sort(key=os.path.getctime)

                    # Add numbering to the recordings
                    numbered_recordings = []
                    for idx, recording in enumerate(recordings, start=1):
                        filename = os.path.basename(recording)
                        numbered_recordings.append((recording, f"{idx}. {filename}"))

                    return numbered_recordings

                recordings_gallery = gr.Gallery(
                    label="Recordings",
                    columns=3,
                    height="auto",
                    object_fit="contain"
                )

                refresh_button = gr.Button("🔄 Refresh Recordings", variant="secondary")
                refresh_button.click(
                    fn=list_recordings,
                    inputs=save_recording_path,
                    outputs=recordings_gallery
                )

            with gr.TabItem("📁 UI Configuration", id=8, visible=False):
                config_file_input = gr.File(
                    label="Load UI Settings from Config File",
                    file_types=[".json"],
                    interactive=True
                )
                with gr.Row():
                    load_config_button = gr.Button("Load Config", variant="primary")
                    save_config_button = gr.Button("Save UI Settings", variant="primary")

                config_status = gr.Textbox(
                    label="Status",
                    lines=2,
                    interactive=False
                )
                save_config_button.click(
                    fn=save_current_config,
                    inputs=[],  # 不需要输入参数
                    outputs=[config_status]
                )

        def save_llm_prefs_and_switch_tab(provider, model_name, api_key):
            # This correctly calls the updated save function which handles the logic
            status = save_llm_preferences(provider, model_name, api_key)
            # Update global keys in memory immediately after saving
            global _global_api_keys
            if provider and api_key:
                 _global_api_keys[provider] = api_key
            return [
                gr.Textbox(value=status, visible=True),
                gr.Tabs(selected=4)
            ]

        # --- Bind Save and Next button click (no change needed here) ---
        save_next_button.click(
            fn=save_llm_prefs_and_switch_tab,
            inputs=[llm_provider, llm_model_name, llm_api_key],
            outputs=[llm_save_status, tabs]
        )
        # Attach the callback to the LLM provider dropdown
        llm_provider.change(
            lambda provider, api_key, base_url: update_model_dropdown(provider, api_key, base_url),
            inputs=[llm_provider, llm_api_key, llm_base_url],
            outputs=llm_model_name
        )

        # Add this after defining the components
        enable_recording.change(
            lambda enabled: gr.update(interactive=enabled),
            inputs=enable_recording,
            outputs=save_recording_path
        )

        use_own_browser.change(fn=close_global_browser)
        keep_browser_open.change(fn=close_global_browser)

        scan_and_register_components(demo)
        global webui_config_manager
        all_components = webui_config_manager.get_all_components()

        load_config_button.click(
            fn=update_ui_from_config,
            inputs=[config_file_input],
            outputs=all_components + [config_status]
        )
    return demo


def main():
    parser = argparse.ArgumentParser(description="sagax1 Browser Agent")
    parser.add_argument("--ip", type=str, default="127.0.0.1", help="IP address to bind to")
    parser.add_argument("--port", type=int, default=7788, help="Port to listen on")
    parser.add_argument("--theme", type=str, default="Ocean", choices=theme_map.keys(), help="Theme to use for the UI")
    args = parser.parse_args()

    demo = create_ui(theme_name=args.theme)
    demo.launch(server_name=args.ip, server_port=args.port)


if __name__ == '__main__':
    main()
