import copy
import json
from pathlib import Path
import requests
import yaml

from managers.github_manager import GithubManager
from managers.orcid_manager import OrcidManager


class CffManager:

    def __init__(
        self, cff_path: str, github_manager: GithubManager, orcid_manager: OrcidManager
    ):
        self.github_manager = github_manager
        self.orcid_manager = orcid_manager
        self.cff_path = cff_path
        self.original_cff = self.load_cff(cff_path=cff_path)

    def load_cff(self, cff_path: str):
        # Check if the CFF file exists
        path: Path = Path(cff_path)
        if not path.exists():
            raise ValueError(f"{cff_path} not found.")

        # make sure the CFF file is valid
        if not self.validate_cff(cff_path):
            raise ValueError(f"Validation failed for input CFF file: {cff_path}")

        # create a dictionary from the CFF file
        with open(path, "r") as f:
            cff = yaml.safe_load(f)

        return cff

    def validate_cff(self, cff_path: str):
        import subprocess

        try:
            subprocess.run(
                ["cffconvert", "--validate", "--infile", cff_path], check=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def is_same_person(self, a: dict, b: dict):
        a_type: str = self.get_cff_author_type(a)
        b_type: str = self.get_cff_author_type(b)

        if a_type == "unknown" or b_type == "unknown":
            raise ValueError("Cannot compare unknown author types.")

        # Ensure comparison is between same types
        if a_type != b_type:
            return False

        if a_type == "entity":
            return (
                a.get("name", "").casefold().strip()
                == b.get("name", "").casefold().strip()
                and a.get("name", "").strip() != ""
            ) or (
                a.get("alias", "").casefold().strip()
                == b.get("alias", "").casefold().strip()
                and a.get("alias", "").strip() != ""
            )

        return (
            (
                a.get("alias", "").casefold().strip()
                == b.get("alias", "").casefold().strip()
                and a.get("alias", "").strip() != ""
            )
            or (
                a.get("email", "").casefold().strip()
                == b.get("email", "").casefold().strip()
                and a.get("email", "").strip() != ""
            )
            or (
                a.get("orcid", "").casefold().strip()
                == b.get("orcid", "").casefold().strip()
                and a.get("orcid", "").strip() != ""
            )
            or (
                f"{a.get('given-names', '').casefold().strip()} {a.get('family-names', '').casefold().strip()}".strip()
                == f"{b.get('given-names', '').casefold().strip()} {b.get('family-names', '').casefold().strip()}".strip()
                and f"{a.get('given-names', '').casefold().strip()} {a.get('family-names', '').casefold().strip()}".strip()
                != ""
            )
        )

    def get_cff_author_type(self, author: dict):
        if "name" in author:
            return "entity"
        elif "given-names" in author and "family-names" in author:
            return "person"
        else:
            return "unknown"

    def get_contribution_note_for_warning(
        self,
        contributor: str | tuple[str, str],
        contribution_details: dict,
        repo_for_compare: str,
    ) -> str:
        """
        Get contribution note for warning.
        Args:
            contributor (str): Contributor name.
            contribution_details (dict): Contribution details.
            repo_for_compare (str): Repository for comparison
        Returns:
            str: Contribution note.
        """
        contribution_categories = [
            ("commits", "Commit"),
            ("pr_comments", "Pull Request Comment"),
            ("reviews", "Review"),
            ("issues", "Issue"),
            ("issue_comments", "Issue Comment"),
        ]

        contribution_note: str = ""
        if isinstance(contributor, tuple):
            contributor_name = contributor[0]
            contributor_email = contributor[1]
            if contributor_email and contributor_name:
                contribution_note = f"- `{contributor_name} <{contributor_email}>`: "
            elif contributor_name:
                contribution_note = f"- `{contributor_name}`: "
            elif contributor_email:
                contribution_note = f"- `{contributor_email}`: "
            else:
                raise ValueError(
                    "Contributor tuple must contain at least one non-empty string."
                )
        else:
            contributor_name = contributor
            if not contributor_name:
                raise ValueError("Contributor name must be a non-empty string.")
            contribution_note = f"- @{contributor_name}: "

        # Find the first contribution category that exists in the contribution details
        # and is not empty

        first_contribution_category: tuple[str, str] | None = None
        for contribution_category in contribution_categories:
            if (
                contribution_category[0] in contribution_details.get(contributor, {})
                and len(contribution_details[contributor][contribution_category[0]]) > 0
            ):
                first_contribution_category = contribution_category
                break
        if first_contribution_category:
            first_contribution = contribution_details[contributor][
                first_contribution_category[0]
            ][0]
            if first_contribution_category[0] == "commits":
                sha = first_contribution
                sha_url = f"https://github.com/{repo_for_compare}/commit/{sha}"
                contribution_note += f"Commit: [`{first_contribution[:7]}`]({sha_url})"
            else:
                contribution_note += (
                    f"[{first_contribution_category[1]}]({first_contribution})"
                )
        else:
            raise Exception("No contribution found for the contributor.")
        return contribution_note

    def update_cff(
        self,
        contributors: set,
        token: str,
        repo: str,
        pr_number: str,
        output_file: str,
        flags: dict,
        repo_for_compare: str,
        contribution_details: dict,
    ) -> set:
        """
        Process contributors and update the CFF file.
        Args:
            contributors (set): Set of contributors.
            token (str): GitHub token.
            repo (str): Repository name.
            pr_number (str): Pull request number.
            output_file (str): Output file path.
            flags (dict): Flags for processing.
            repo_for_compare (str): Repository for comparison.
            contribution_details (dict): Contribution details.
        """
        if not isinstance(contributors, set):
            raise ValueError("Contributors must be a set.")

        if not token:
            raise ValueError("GitHub token is not provided.")

        if not repo:
            raise ValueError("Repository is not provided.")

        if not pr_number:
            raise ValueError("Pull request number is not provided.")

        if not output_file:
            raise ValueError("Output file path is not provided.")

        cff = copy.deepcopy(self.original_cff)

        cff.setdefault("authors", [])

        warnings: list = []
        logs: list = []

        already_in_cff_contributors: set = set()

        for contributor in contributors:
            entry: dict = {}
            identifier: str = ""

            contribution_note = self.get_contribution_note_for_warning(
                contributor=contributor,
                contribution_details=contribution_details,
                repo_for_compare=repo_for_compare,
            )

            if isinstance(contributor, str):
                # Github user contributor
                identifier: str = contributor.casefold()
                user_url: str = f"https://api.github.com/users/{contributor}"
                resp: requests.Response = requests.get(
                    user_url, headers={"Authorization": f"token {token}"}
                )

                # skip the user if the user is not found
                if resp.status_code != 200:
                    warnings.append(
                        f"- @{contributor}: Unable to fetch user data from GitHub API. Status code: {resp.status_code}{contribution_note}"
                    )
                    continue

                # determine the type of user
                user = resp.json()
                user_type = user.get("type")
                user_profile_url: str = (
                    f"https://github.com/{contributor.casefold().strip()}"
                )

                if user_type == "Organization":
                    entry["name"] = user.get("name") or contributor
                    entry["alias"] = user_profile_url
                    if user.get("email"):
                        entry["email"] = user["email"]
                else:
                    full_name: str = user.get("name") or contributor
                    name_parts: list[str] = full_name.split(" ", 1)

                    if len(name_parts) > 1:
                        entry["given-names"] = name_parts[0]
                        entry["family-names"] = name_parts[1]
                        entry["alias"] = user_profile_url
                    else:
                        entry["name"] = full_name
                        entry["alias"] = user_profile_url
                        warnings.append(
                            f"- @{contributor}: Only one name part found, treated as entity for deduplication consistency.{contribution_note}"
                        )
                        if any(self.is_same_person(a, entry) for a in cff["authors"]):
                            already_in_cff_contributors.add(contributor)
                            warnings.append(
                                f"- {identifier}: Already exists in CFF file."
                            )
                            continue
                        cff["authors"].append(entry)
                        continue

                    if user.get("email"):
                        entry["email"] = user["email"]
                    orcid = self.orcid_manager.extract_orcid(text=user.get("bio"))
                    if not orcid and full_name:
                        orcid = self.orcid_manager.search_orcid(
                            full_name=full_name, email=user.get("email"), logs=logs
                        )
                    if orcid and self.orcid_manager.validate_orcid(orcid=orcid):
                        entry["orcid"] = f"https://orcid.org/{orcid}"
                    elif orcid:
                        warnings.append(
                            f"- @{contributor}: ORCID `{orcid}` is invalid or unreachable."
                        )
                    else:
                        warnings.append(f"- @{contributor}: No ORCID found.")

            else:
                name, email = contributor
                name_parts: list[str] = name.split(" ", 1)
                full_name: str = name
                entry_type: str = "entity"
                matched: bool = False

                for existing in cff["authors"]:
                    if (
                        "given-names" in existing
                        and "family-names" in existing
                        and "email" in existing
                    ):
                        existing_name = f"{existing['given-names']} {existing['family-names']}".strip().casefold()
                        if (
                            existing_name == name.strip().casefold()
                            and existing["email"].strip().casefold()
                            == email.strip().casefold()
                        ):
                            entry["given-names"] = existing["given-names"]
                            entry["family-names"] = existing["family-names"]
                            entry["email"] = existing["email"]
                            if "orcid" in existing:
                                entry["orcid"] = existing["orcid"]
                            entry_type = "person"
                            matched = True
                            break

                if entry_type == "entity" and not matched:
                    if not name.strip():
                        warnings.append(
                            f"- Commit author with email `{email}` has no name and was skipped.{contribution_note}"
                        )
                        continue
                    entry["name"] = name
                    if email:
                        entry["email"] = email
                else:
                    if len(name_parts) > 1:
                        entry["given-names"] = name_parts[0]
                        entry["family-names"] = name_parts[1]
                        if email:
                            entry["email"] = email
                        orcid = self.orcid_manager.search_orcid(full_name, email, logs)
                        if orcid and self.orcid_manager.validate_orcid(orcid):
                            entry["orcid"] = f"https://orcid.org/{orcid}"
                        elif orcid:
                            warnings.append(
                                f"- `{full_name}`: ORCID `{orcid}` is invalid or unreachable."
                            )
                        else:
                            warnings.append(f"- `{full_name}`: No ORCID found.")
                    else:
                        entry["name"] = full_name
                        if email:
                            entry["email"] = email
                        warnings.append(
                            f"- `{full_name}`: Only one name part found, treated as entity for deduplication consistency.{contribution_note}"
                        )

                identifier = email or name.casefold()

            if any(self.is_same_person(a, entry) for a in cff["authors"]):
                already_in_cff_contributors.add(contributor)
                warnings.append(f"- {identifier}: Already exists in CFF file.")
                continue

            cff["authors"].append(entry)

        with open(self.cff_path, "w") as f:
            yaml.dump(cff, f, sort_keys=False)

        if not self.validate_cff(self.cff_path):
            raise Exception(f"Validation failed for output CFF file: {self.cff_path}")

        with open(output_file, "a") as f:
            f.write("new_authors<<EOF\n")
            f.write(
                self.create_json_for_contribution_details(
                    contribution_details=contribution_details
                )
            )
            f.write("\nEOF\n")
            f.write("updated_cff<<EOF\n")
            f.write(yaml.dump(cff, sort_keys=False))
            f.write("\nEOF\n")
            if warnings:
                f.write("warnings<<EOF\n" + "\n".join(warnings) + "\nEOF\n")
            if logs:
                f.write("orcid_logs<<EOF\n" + "\n".join(logs) + "\nEOF\n")

        missing_authors: set = contributors - already_in_cff_contributors
        if flags["post_comment"] and pr_number:
            self.github_manager.post_pull_request_comment(
                cff_path=self.cff_path,
                cff=cff,
                warnings=warnings,
                logs=logs,
                token=token,
                repo=repo,
                pr_number=pr_number,
                contribution_details=contribution_details,
                repo_for_compare=repo_for_compare,
                missing_authors=missing_authors,
                missing_author_invalidates_pr=flags["missing_author_invalidates_pr"],
            )

        return missing_authors

    def create_json_for_contribution_details(self, contribution_details: dict) -> str:
        normalized = []
        for contributor, contributions in contribution_details.items():
            normalized_contributor = {
                "contributor": contributor,
                "contributions": contributions,
            }
            normalized.append(normalized_contributor)
        return json.dumps(normalized)
