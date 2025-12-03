#pragma once

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joy.hpp>
#include <vector>
#include <string>
#include <sstream>

class JoystickControl : public rclcpp::Node
{
public:
    JoystickControl();

private:
    void joy_callback(const sensor_msgs::msg::Joy::SharedPtr msg);
    void move(const std::vector<int>& value);

    rclcpp::Subscription<sensor_msgs::msg::Joy>::SharedPtr joy_sub_;

    bool l1_held = false;  // Used to detect if L1 is being held
};
