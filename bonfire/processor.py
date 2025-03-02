import copy
import logging
import json
import yaml
import re
import traceback
import uuid

from pathlib import Path

import bonfire.config as conf
from bonfire.openshift import process_template, whoami
from bonfire.utils import RepoFile, FatalError
from bonfire.utils import get_dependencies as utils_get_dependencies

log = logging.getLogger(__name__)


def _remove_resource_config(items):
    for i in items:
        if i["kind"] != "ClowdApp":
            continue

        removed = False
        for d in i["spec"].get("deployments", []):
            if "resources" in d["podSpec"]:
                del d["podSpec"]["resources"]
                removed = True
        for p in i["spec"].get("pods", []):
            if "resources" in p:
                del p["resources"]
                removed = True

        if removed:
            log.debug("removed resources from ClowdApp '%s'", i["metadata"]["name"])


def _set_replicas(items):
    for i in items:
        if i["kind"] != "ClowdApp":
            continue

        updated = False
        for d in i["spec"].get("deployments", []):
            if "minReplicas" in d["podSpec"] and d["podSpec"]["minReplicas"] > 1:
                d["podSpec"]["minReplicas"] = 1
                updated = True
        for p in i["spec"].get("pods", []):
            if "minReplicas" in p and p["minReplicas"] > 1:
                p["minReplicas"] = 1
                updated = True

        if updated:
            log.debug("set replicas to '1' on ClowdApp '%s'", i["metadata"]["name"])


def process_clowd_env(target_ns, quay_user, env_name, template_path):
    log.info("processing ClowdEnvironment")

    env_template_path = Path(template_path if template_path else conf.DEFAULT_CLOWDENV_TEMPLATE)

    if not env_template_path.exists():
        raise FatalError("ClowdEnvironment template file does not exist: %s", env_template_path)

    with env_template_path.open() as fp:
        template_data = yaml.safe_load(fp)

    params = dict()
    params["ENV_NAME"] = env_name
    if quay_user:
        quay_user = quay_user.replace("_", "-")
        params["PULL_SECRET_NAME"] = f"{quay_user}-pull-secret"
    if target_ns:
        params["NAMESPACE"] = target_ns

    processed_template = process_template(template_data, params=params)

    if not processed_template.get("items"):
        raise FatalError("Processed ClowdEnvironment template has no items")

    return processed_template


def process_iqe_cji(
    clowd_app_name,
    debug=False,
    marker="",
    filter="",
    env="clowder_smoke",
    image_tag="",
    cji_name=None,
    template_path=None,
    requirements="",
    requirements_priority="",
    test_importance="",
):
    log.info("processing IQE ClowdJobInvocation")

    template_path = Path(template_path if template_path else conf.DEFAULT_IQE_CJI_TEMPLATE)

    if not template_path.exists():
        raise FatalError("CJI template file does not exist: %s", template_path)

    with template_path.open() as fp:
        template_data = yaml.safe_load(fp)

    requirements = requirements.split(",") if requirements else []
    requirements_priority = requirements_priority.split(",") if requirements_priority else []
    test_importance = test_importance.split(",") if test_importance else []

    params = dict()
    params["DEBUG"] = str(debug).lower()
    params["MARKER"] = marker
    params["FILTER"] = filter
    params["ENV_NAME"] = env
    params["IMAGE_TAG"] = image_tag
    params["NAME"] = cji_name or f"iqe-{str(uuid.uuid4()).split('-')[0]}"
    params["APP_NAME"] = clowd_app_name
    params["REQUIREMENTS"] = json.dumps(requirements)
    params["REQUIREMENTS_PRIORITY"] = json.dumps(requirements_priority)
    params["TEST_IMPORTANCE"] = json.dumps(test_importance)

    processed_template = process_template(template_data, params=params)

    if not processed_template.get("items"):
        raise FatalError("Processed CJI template has no items")

    return processed_template


def process_reservation(
    name,
    requester,
    duration,
    template_path=None,
):
    log.info("processing namespace reservation")

    template_path = Path(template_path if template_path else conf.DEFAULT_RESERVATION_TEMPLATE)

    if not template_path.exists():
        raise FatalError("Reservation template file does not exist: %s", template_path)

    with template_path.open() as fp:
        template_data = yaml.safe_load(fp)

    params = dict()

    params["NAME"] = name if name else f"bonfire-reservation-{str(uuid.uuid4()).split('-')[0]}"
    params["DURATION"] = duration

    if requester is None:
        try:
            requester = whoami()
        except Exception:
            log.info("whoami returned an error - setting requester to 'bonfire'")  # minikube
            requester = "bonfire"

    params["REQUESTER"] = requester

    processed_template = process_template(template_data, params=params)

    if not processed_template.get("items"):
        raise FatalError("Processed Reservation template has no items")

    return processed_template


class TemplateProcessor:
    @staticmethod
    def _parse_app_names(app_names):
        parsed_app_names = set()
        for app_name in app_names:
            # backward compatibility for specifying app names with comma,separated,values
            for entry in app_name.split(","):
                parsed_app_names.add(entry)
        return parsed_app_names

    @staticmethod
    def _find_dupe_components(components_for_app):
        """Make sure no component is listed more than once across all apps."""
        for app_name, components in components_for_app.items():
            components_for_other_apps = copy.copy(components_for_app)
            del components_for_other_apps[app_name]

            for component in components:
                found_in = [app_name]
                for other_app_name, other_components in components_for_other_apps.items():
                    if component in other_components:
                        found_in.append(other_app_name)
                if len(found_in) > 1:
                    raise FatalError(
                        f"component '{component}' is not unique, found in apps: {found_in}"
                    )

    @staticmethod
    def _validate_component_dict(all_components, data, name):
        # Translate deprecated "APP_NAME/COMPONENT_NAME" path syntax
        updated_data = {}

        # Max number of items we expect separated by '/' in the CLI option value
        max_len = 2
        if name == "--set-parameter":
            max_len = 3

        for path, value in data.items():
            split = path.split("/")
            if len(split) == max_len:
                # first item was an app name
                new_path = split[1:]
            elif len(split) == max_len - 1:
                # first item was a component name
                new_path = split[0:]
            else:
                raise FatalError(f"invalid format for {name}: {path}={value}")

            component_name = new_path[0]

            # Make sure component name actually exists in app config
            if component_name not in all_components:
                raise FatalError(
                    f"component given for {name} not found in app config: {component_name}"
                )

            key = "/".join(new_path)
            updated_data[key] = value

        # Update the paths
        data.clear()
        for key, val in updated_data.items():
            data[key] = val

    @staticmethod
    def _validate_component_list(all_components, data, name):
        for component_name in data:
            if component_name not in all_components:
                raise FatalError(
                    f"component given for {name} not found in app config: {component_name}"
                )

    def _validate_component_options(self, all_components, data, name):
        if isinstance(data, dict):
            self._validate_component_dict(all_components, data, name)
        else:
            self._validate_component_list(all_components, data, name)

    def _validate(self):
        """
        Validate app configurations and options passed to the TemplateProcessor

        1. Check that each app has required keys
        2. Check that each app name is unique
        3. Check that each component in an app has required keys
        4. Check that each component is a unique name across the whole config
        5. Check that CLI params requiring a component use a valid component name
        """
        components_for_app = {}

        for app_name, app_cfg in self.apps_config.items():
            # Check that each app has required keys
            required_keys = ["name", "components"]
            missing_keys = [k for k in required_keys if k not in app_cfg]
            if missing_keys:
                raise FatalError(f"app '{app_name}' is missing required keys: {missing_keys}")

            # Check that each app name is unique
            app_name = app_cfg["name"]
            if app_name in components_for_app:
                raise FatalError(f"app with name '{app_name}' is not unique")
            components_for_app[app_name] = []

            for component in app_cfg.get("components", []):
                # Check that each component in an app has required keys
                required_keys = ["name", "host", "repo", "path"]
                missing_keys = [k for k in required_keys if k not in component]
                if missing_keys:
                    raise FatalError(
                        f"component on app {app_name} is missing required keys: {missing_keys}"
                    )
                comp_name = component["name"]
                components_for_app[app_name].append(comp_name)

        # Check that each component name is unique across the whole config
        self._find_dupe_components(components_for_app)

        # Check that CLI params requiring a component use a valid component name
        all_components = []
        for _, app_components in components_for_app.items():
            all_components.extend(app_components)

        log.debug("components found: %s", all_components)

        self._validate_component_options(
            all_components, self.template_ref_overrides, "--set-template-ref"
        )
        self._validate_component_options(all_components, self.param_overrides, "--set-parameter")

        # 'all' is a valid component keyword for these options below
        all_components.append("all")

        self._validate_component_options(
            all_components, self.remove_resources, "--remove-resources"
        )
        self._validate_component_options(
            all_components, self.no_remove_resources, "--no-remove-resources"
        )
        self._validate_component_options(all_components, self.component_filter, "--component")

    def __init__(
        self,
        apps_config,
        app_names,
        get_dependencies,
        image_tag_overrides,
        template_ref_overrides,
        param_overrides,
        clowd_env,
        remove_resources,
        no_remove_resources,
        single_replicas,
        component_filter,
    ):
        self.apps_config = apps_config
        self.requested_app_names = self._parse_app_names(app_names)
        self.get_dependencies = get_dependencies
        self.image_tag_overrides = image_tag_overrides
        self.template_ref_overrides = template_ref_overrides
        self.param_overrides = param_overrides
        self.clowd_env = clowd_env
        self.remove_resources = remove_resources
        self.no_remove_resources = no_remove_resources
        self.single_replicas = single_replicas
        self.component_filter = component_filter

        self._validate()

        self.k8s_list = {
            "kind": "List",
            "apiVersion": "v1",
            "metadata": {},
            "items": [],
        }

        self.processed_components = set()

    def _get_app_config(self, app_name):
        if app_name not in self.apps_config:
            raise FatalError(f"app {app_name} not found in apps config")
        return self.apps_config[app_name]

    def _get_component_config(self, component_name):
        for _, app_cfg in self.apps_config.items():
            for component in app_cfg["components"]:
                if component["name"] == component_name:
                    return component
        else:
            raise FatalError(f"component with name '{component_name}' not found")

    def _sub_image_tags(self, items):
        content = json.dumps(items)
        for image, image_tag in self.image_tag_overrides.items():
            # easier to just re.sub on a whole string
            content, subs = re.subn(rf"{image}:[-\w\.]+", rf"{image}:{image_tag}", content)
            if subs:
                log.info("replaced %d occurence(s) of image tag for image '%s'", subs, image)
        return json.loads(content)

    def _sub_ref(self, current_component_name, repo_file):
        for component_name, value in self.template_ref_overrides.items():
            if current_component_name == component_name:
                log.info(
                    "component: '%s' overriding template ref to '%s'",
                    component_name,
                    value,
                )
                repo_file.ref = value

    def _sub_params(self, current_component_name, params):
        for path, value in self.param_overrides.items():
            split = path.split("/")
            if len(split) == 2:
                component_name, param_name = split
            else:
                raise FatalError(f"invalid format for --set-parameter: {path}={value}")

            if current_component_name == component_name:
                log.info(
                    "component: '%s' overriding param '%s' to '%s'",
                    component_name,
                    param_name,
                    value,
                )
                params[param_name] = value

    def _get_component_items(self, component_name):
        component = self._get_component_config(component_name)
        try:
            rf = RepoFile.from_config(component)
            # override template ref if requested
            self._sub_ref(component_name, rf)
            commit, template_content = rf.fetch()
        except Exception as err:
            log.error("failed to fetch template file for %s", component_name)
            log.debug(traceback.format_exc())
            raise FatalError(err)

        template = yaml.safe_load(template_content)

        params = {
            "IMAGE_TAG": commit[:7],
            "ENV_NAME": self.clowd_env,
        }

        params.update(component.get("parameters", {}))

        # override any specific parameters on this component if requested
        self._sub_params(component_name, params)

        new_items = process_template(template, params)["items"]

        # override the tags for all occurences of an image if requested
        new_items = self._sub_image_tags(new_items)

        if (
            "all" not in self.no_remove_resources
            and ("all" in self.remove_resources or component_name in self.remove_resources)
            and component_name not in self.no_remove_resources
        ):
            _remove_resource_config(new_items)
        if self.single_replicas:
            _set_replicas(new_items)

        return new_items

    def _process_component(self, component_name):
        if component_name not in self.processed_components:
            log.info("processing component %s", component_name)
            new_items = self._get_component_items(component_name)
            self.k8s_list["items"].extend(new_items)

            self.processed_components.add(component_name)

            if self.get_dependencies:
                # recursively process components to add config for dependent apps to self.k8s_list
                self._add_dependencies_to_config(component_name, new_items)
        else:
            log.debug("component %s already processed", component_name)

    def _add_dependencies_to_config(self, component_name, new_items):
        dependencies = set()
        for _, deps in utils_get_dependencies(new_items).items():
            dependencies = dependencies.union(deps)

        # filter out ones we've already processed before
        dependencies = [d for d in dependencies if d not in self.processed_components]

        if dependencies:
            log.info("dependencies not previously processed: %s", dependencies)
            for component_name in dependencies:
                self._process_component(component_name)

    def _process_app(self, app_name):
        log.info("processing app '%s'", app_name)
        app_cfg = self._get_app_config(app_name)
        for component in app_cfg["components"]:
            component_name = component["name"]
            log.debug("app '%s' has component '%s'", app_name, component_name)
            if self.component_filter and component_name not in self.component_filter:
                log.debug(
                    "skipping component '%s', not found in --component filter", component_name
                )
                continue
            self._process_component(component_name)

    def process(self, app_names=None):
        if not app_names:
            app_names = self.requested_app_names

        for app_name in app_names:
            self._process_app(app_name)

        return self.k8s_list
