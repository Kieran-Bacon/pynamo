
import typing

import decimal
import enum

from typing import Dict, Any, Protocol, runtime_checkable, Tuple, Any

@runtime_checkable
class ISOFormatable(Protocol):

    def isoformat(self):
        pass



@runtime_checkable
class NPNDarrayLike(Protocol):

    @property
    def shape(self) -> Any:
        ...


    def tolist(self) -> Any:
        ...




def _sanitiseItem(item: Dict[Any, Any]) -> Dict[str, Any]:

    for key, value in item.items():
        if isinstance(value, Dict):
            item[key] = _sanitiseItem(value)

        elif isinstance(value, enum.Enum):
            item[key] = value.value

        elif isinstance(value, NPNDarrayLike):
            if value.shape == ():
                # This a single value held in an array
                val = value.tolist()
                if isinstance(val, float):
                    val = decimal.Decimal(str(value))
                item[key] = val

            else:
                item[key] = list(_sanitiseItem({i: v for i, v in enumerate(value.tolist())}).values())

        elif isinstance(value, (typing.List, typing.Set)):
            # NOTE relying on the order of dictionaries
            # https://medium.com/junior-dev/python-dictionaries-are-ordered-now-but-how-and-why-5d5a40ee327f
            item[key] = list(_sanitiseItem({i: v for i, v in enumerate(value)}).values())

        elif isinstance(value, float):
            item[key] = decimal.Decimal(str(value))

        elif isinstance(value, ISOFormatable):
            item[key] = value.isoformat()

    return item



def generate_change_dict(old: dict, new: dict) -> dict:
    """ Generate a dictionary from the the key, vals in `new` which have changed frol `old`

        Keys whose values have not changed are not present in the output
    """

    output = {}

    for key, val in new.items():

        old_val = old[key]

        if isinstance(val, dict):
            diff = generate_change_dict(old_val, val)

            if len(diff) > 0:
                output[key] = diff

            continue

        if old_val != val:
            output[key] = val


    return output

def build_set_statement(change_dict: dict, prefix = []) -> typing.Tuple[str, dict, dict]:
    """ Creates a SET statetement for use with boto3 `update_item` method based on the supplied change_dict

        Returns:
            (str): the SET statement which applies the changes described in the `change_dict`
            dict: a dictionary of attribute names which can be passed as `ExpressionAttributeNames`
            dict: a dictionary of attribute values which can be passed as `ExpressionAttributeValues`

    """

    expression = "" if prefix else "SET "

    attr_names = {}
    attr_values = {}

    for i, (key, val) in enumerate(change_dict.items()):

        _id = 'v' + str(i)

        name_seq = prefix[-1] + '_' if len(prefix) > 0 else ""

        address_seq = ("#" + ".#".join(prefix) + '.') if prefix else ""

        key_name = '#' + name_seq + _id

        attr_names[key_name] = key

        if isinstance(val, dict):

            expr, names, values = build_set_statement(val, prefix=prefix + [name_seq+ _id])

            expression += expr + ", "

            attr_names.update(names)
            attr_values.update(values)

            continue


        val_name = ':' + name_seq + _id
        attr_values[val_name] = val

        expression += "{}={}, ".format(address_seq + key_name if prefix else key_name, val_name)


    return expression[:-2], attr_names, _sanitiseItem(attr_values)




