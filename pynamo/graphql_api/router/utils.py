import re


from typing import (
    Union,
    List,
    Dict,
)


def convert_to_snake_case(data: Union[str, List, Dict]) -> Union[str, List, Dict]:

    if isinstance(data, str):
        return re.sub(r"([A-Z])", r"_\1", data).lower()

    if isinstance(data, List):
        new_list = [
            convert_to_snake_case(v) if isinstance(v, Dict) else v for v in data
        ]

        return new_list

    if isinstance(data, Dict):
        new_dict = {}
        for key, val in data.items():

            new_dict[convert_to_snake_case(key)] = (
                convert_to_snake_case(val)
                if isinstance(val, Dict) or isinstance(val, List)
                else val
            )

        return new_dict

    return data


def convert_to_camel_case(data: Union[str, List, Dict]) -> Union[str, List, Dict]:

    if isinstance(data, str):
        new_str = data

        # To protect against keys that are emun values like BRONZE
        if not data.isupper():

            new_str = re.sub(r"(_|-)+", " ", data).title().replace(" ", "")

            new_str = new_str[0].lower() + new_str[1:]

        return new_str

    if isinstance(data, List):
        new_list = [
            convert_to_camel_case(v) if isinstance(v, Dict) else v for v in data
        ]

        return new_list

    if isinstance(data, Dict):
        new_dict = {}
        for key, val in data.items():

            new_dict[convert_to_camel_case(key)] = (
                convert_to_camel_case(val)
                if isinstance(val, Dict) or isinstance(val, List)
                else val
            )

        return new_dict

    return data


def sanitiseTypeNames(python_name: str) -> str:
    """Takes a python compatible type name as input and returns one compatible with GQL

    Example: PaginatedResult[PartnerInvitation] -> PaginatedResult_PartnerInvitation
    """

    return python_name.replace("[", "_").replace("]", "")
